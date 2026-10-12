"""Offline DR, Round 4 step 1: restore a signed backup into NEW staging slots.

This is the first half of the cutover gate described in restore-runbook.md.
It never touches the live database or the live media volumes:

* the database is restored into a NEW PostgreSQL database that must not exist;
* public and private media are extracted into NEW directories that must not
  exist and may not overlap the live roots, the backup or each other;
* every step is written to an append-only journal (one JSON object per line,
  fsynced together with its directory) that lives outside the database, so a
  SIGKILL leaves an exact record of what was created and how far it got.

The staged slots are then proved against the signed manifest: the applied
migrations and the business-data fingerprint of the staged database must equal
the ones recorded at backup time, and every file referenced by the staged
database must exist in the staged media.

There is deliberately NO cutover here: switching the application to the staged
slots is the separate operator procedure in ``offline_cutover``.
``discard_stage`` removes only what the journal says this tool created, and
refuses while a cutover of this stage is not rolled back.

Every DR tool (staging, discard, cutover) holds one exclusive operation lock:
``flock`` on a lock file plus a session advisory lock in PostgreSQL.  Both are
released by the kernel or the server when the process dies, so a SIGKILL never
leaves a stale lock behind.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import socket
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from django.apps import apps
from django.conf import settings
from django.db import connection, connections, models
from django.utils import timezone
from psycopg import sql

from . import backup
from .dr_faults import fault_point
from .emergency_manifest import validate_manifest
from .emergency_state import (
    business_state_marker,
    media_tree_sha256,
    migration_state,
    sha256_file,
)
from .manifest_signing import ManifestSignatureError, verify_manifest
from .private_media import Inventory, PrivateMediaError, archive_inventory, restore_archive

DATABASE_NAME_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
MISSING_FILE_SAMPLE = 20
# Cluster-wide: two DR tools on the same PostgreSQL never run together.
DR_ADVISORY_LOCK_ID = 0x44454E5344524C4B
CUTOVER_JOURNAL_SUFFIX = ".cutover"
TORN_TAIL_STAGE = "torn_tail_sealed"


class StageRestoreError(Exception):
    pass


class JournalCorruptError(StageRestoreError):
    pass


@dataclass(frozen=True)
class StageTargets:
    database: str
    media_dir: Path
    private_media_dir: Path
    journal: Path


@dataclass(frozen=True)
class ExternalPrivateMedia:
    """A private_media archive captured next to (not inside) an older backup."""

    archive: Path
    expected: Inventory


def durable_fsync(fd: int) -> None:
    """fsync that also flushes the drive cache where the OS separates the two.

    On macOS ``fsync`` only hands data to the drive; ``F_FULLFSYNC`` asks the
    drive to persist it.  Linux ``fsync`` already does both.
    """
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def sync_directory(path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        durable_fsync(fd)
    finally:
        os.close(fd)


class Journal:
    """Append-only, fsynced JSON-lines record; created exclusively."""

    def __init__(self, path: Path):
        self.path = Path(path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        os.close(fd)
        self._sync_dir()

    def _sync_dir(self) -> None:
        sync_directory(self.path.parent)

    def record(self, stage: str, **data) -> None:
        line = json.dumps(
            {"at": timezone.now().isoformat(timespec="seconds"), "stage": stage, **data},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        # One write() of a whole line, then fsync: the next action starts only
        # after the record is on disk.
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        payload = (line + "\n").encode("utf-8")
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError("Журнал записан не полностью (диск заполнен?).")
            durable_fsync(fd)
        finally:
            os.close(fd)
        self._sync_dir()
        fault_point(f"journal:{stage}")


def read_journal(path) -> list[dict]:
    """Parse the journal; tolerate only a torn last record, never guess.

    A record is written as one line and fsynced before the action it
    announces, so bytes after the last newline (power loss or a short write)
    were never acted on and are ignored.  ``ExistingJournal`` seals such a
    tail with a newline and a ``torn_tail_sealed`` record before appending;
    the line right before that record is void.  Any other malformed line makes
    the journal untrustworthy: ``JournalCorruptError``.
    """
    lines = Path(path).read_bytes().split(b"\n")[:-1]  # last piece: torn or empty
    entries: list[tuple[int, dict | None]] = []
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
            if not isinstance(row, dict) or not isinstance(row.get("stage"), str):
                raise ValueError("not a journal record")
        except ValueError:
            row = None
        if row is not None and row["stage"] == TORN_TAIL_STAGE:
            if not entries:
                raise JournalCorruptError(f"Журнал повреждён: строка {number}.")
            entries.pop()  # the sealed fragment was never acted on
        entries.append((number, row))
    rows = []
    for number, row in entries:
        if row is None:
            raise JournalCorruptError(f"Журнал повреждён: строка {number}.")
        rows.append(row)
    return rows


def journal_has_torn_tail(path) -> bool:
    data = Path(path).read_bytes()
    return bool(data) and not data.endswith(b"\n")


# --- Exclusive DR operation lock --------------------------------------------


def lock_path() -> Path:
    configured = str(getattr(settings, "DENSTOCK_DR_LOCK_PATH", "") or "").strip()
    return Path(configured) if configured else backup.backup_root() / ".dr-operation.lock"


def lock_holder() -> str | None:
    """Who holds the lock right now (None when free); read-only probe."""
    path = lock_path()
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return path.read_text(encoding="utf-8", errors="replace").strip() or "?"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


@contextmanager
def dr_operation_lock(settings_dict: dict, *, database: bool = True):
    """Refuse a second DR operation while one runs (pid/host recorded)."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = path.read_text(encoding="utf-8", errors="replace").strip() or "?"
            raise StageRestoreError(
                f"Другая DR-операция уже выполняется ({holder}). Дождитесь её завершения."
            ) from None
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({
                "pid": os.getpid(), "host": socket.gethostname(),
                "started_at": timezone.now().isoformat(timespec="seconds"),
            }).encode("utf-8"))
            durable_fsync(fd)
            with _advisory_lock(settings_dict, enabled=database):
                yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def _advisory_lock(settings_dict: dict, *, enabled: bool):
    if not enabled or "postgresql" not in str(settings_dict.get("ENGINE", "")):
        yield
        return
    with psycopg.connect(**_admin_params(settings_dict)) as admin:
        got = admin.execute("SELECT pg_try_advisory_lock(%s)", (DR_ADVISORY_LOCK_ID,))
        if not got.fetchone()[0]:
            raise StageRestoreError("Другая DR-операция уже держит блокировку в PostgreSQL.")
        yield


# --- Guards (nothing is created before all of them pass) ---------------------


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _check_targets(run_dir: Path, targets: StageTargets, s: dict) -> None:
    if "postgresql" not in s["ENGINE"]:
        raise StageRestoreError("Staging restore поддерживает только PostgreSQL.")
    if not DATABASE_NAME_RE.fullmatch(targets.database):
        raise StageRestoreError(
            "Имя staging-базы: строчные латинские буквы, цифры и _, до 63 символов."
        )
    if targets.database in {str(s.get("NAME") or ""), "postgres", "template0", "template1"}:
        raise StageRestoreError("Staging-база не может совпадать с рабочей или системной базой.")

    live = {
        "MEDIA_ROOT": Path(settings.MEDIA_ROOT).resolve(),
        "PRIVATE_MEDIA_ROOT": Path(settings.PRIVATE_MEDIA_ROOT).resolve(),
        "BACKUP_ROOT": backup.backup_root().resolve(),
        "каталог бэкапа": run_dir.resolve(),
    }
    media = targets.media_dir.resolve()
    private = targets.private_media_dir.resolve()
    journal = targets.journal.resolve()
    for label, path in (("media", media), ("private_media", private)):
        if path.exists() or path.is_symlink():
            raise StageRestoreError(f"Staging-каталог {label} уже существует: {path}")
        if not path.parent.is_dir():
            raise StageRestoreError(f"Родительский каталог для {label} не найден: {path.parent}")
        for live_label, live_path in live.items():
            if _overlaps(path, live_path):
                raise StageRestoreError(
                    f"Staging-каталог {label} пересекается с {live_label}: {path}"
                )
    if _overlaps(media, private):
        raise StageRestoreError("Staging-каталоги media и private_media пересекаются.")
    if journal.exists() or journal.is_symlink():
        raise StageRestoreError(f"Журнал уже существует: {journal}")
    if not journal.parent.is_dir():
        raise StageRestoreError(f"Каталог журнала не найден: {journal.parent}")
    for path in (media, private, *live.values()):
        if journal.is_relative_to(path):
            raise StageRestoreError("Журнал не может лежать внутри staging- или рабочих каталогов.")


def _verify_inputs(run_dir: Path, private_media: ExternalPrivateMedia | None) -> dict:
    validation = validate_manifest(run_dir, expected_source="production")
    if not validation.ok:
        raise StageRestoreError("Manifest не прошёл проверку: " + "; ".join(validation.errors))
    manifest = validation.manifest
    try:
        verify_manifest(manifest)  # mandatory here in every mode
    except ManifestSignatureError as exc:
        raise StageRestoreError(f"Подпись manifest: {exc}") from exc
    for name, expected in (manifest.get("sha256") or {}).items():
        path = run_dir / name
        if not path.is_file() or sha256_file(path) != expected:
            raise StageRestoreError(f"Контрольная сумма {name} не совпадает.")

    status = manifest.get("private_media_status")
    if status == "included" and private_media is not None:
        raise StageRestoreError(
            "В бэкапе уже есть private_media: внешний архив не принимается."
        )
    if status != "included" and private_media is None:
        raise StageRestoreError(
            "В бэкапе нет private_media: это не полный DR-набор. Укажите отдельно "
            "снятый архив private_media и его инвентарь."
        )
    if private_media is not None:
        try:
            packed = archive_inventory(private_media.archive)
        except PrivateMediaError as exc:
            raise StageRestoreError(str(exc)) from exc
        if packed != private_media.expected:
            raise StageRestoreError("Внешний архив private_media не совпадает с инвентарём.")
    return manifest


# --- Database helpers ---------------------------------------------------------


def _admin_params(s: dict) -> dict:
    return {
        "host": s.get("HOST") or "localhost",
        "port": s.get("PORT") or 5432,
        "user": s.get("USER") or "",
        "password": s.get("PASSWORD") or "",
        "dbname": "postgres",
        "autocommit": True,
    }


def _create_database(s: dict, name: str) -> int:
    with psycopg.connect(**_admin_params(s)) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if exists:
            raise StageRestoreError(f"База {name} уже существует: staging только в новую базу.")
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        return admin.execute(
            "SELECT oid FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()[0]


def database_oid(s: dict, name: str) -> int | None:
    with psycopg.connect(**_admin_params(s)) as admin:
        row = admin.execute("SELECT oid FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row[0] if row else None


def _drop_database(s: dict, name: str) -> None:
    with psycopg.connect(**_admin_params(s)) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


class _StagedAlias:
    """Temporary Django connection alias pointing at the staged database."""

    def __init__(self, s: dict, name: str):
        self.alias = f"dr_stage_{uuid.uuid4().hex[:12]}"
        self.config = {**s, "NAME": name}

    def __enter__(self) -> str:
        connections.databases[self.alias] = self.config
        return self.alias

    def __exit__(self, *exc) -> None:
        try:
            connections[self.alias].close()
        finally:
            del connections[self.alias]
            connections.databases.pop(self.alias, None)


# --- Verification of the staged slots ----------------------------------------


def _file_fields():
    for model in apps.get_models():
        if not model._meta.managed:
            continue
        for field in model._meta.concrete_fields:
            if isinstance(field, models.FileField):
                yield model, field


def _is_private_storage(field) -> bool:
    location = getattr(field.storage, "location", None)
    return bool(location) and Path(location).resolve() == Path(
        settings.PRIVATE_MEDIA_ROOT
    ).resolve()


def check_file_references(alias: str, media_dir: Path, private_dir: Path) -> dict:
    """Every non-empty FileField value in the staged DB must exist in a staged slot.

    Private attachments may still sit in the public tree (the storage keeps a
    legacy fallback for queued rows); those are counted as ``legacy_public``.
    """
    checked, legacy, missing = 0, 0, []
    for model, field in _file_fields():
        names = (
            model._base_manager.using(alias)
            .exclude(**{field.attname: ""})
            .exclude(**{f"{field.attname}__isnull": True})
            .values_list(field.attname, flat=True)
        )
        private = _is_private_storage(field)
        for name in names.iterator(chunk_size=1000):
            checked += 1
            primary = (private_dir if private else media_dir) / name
            if primary.is_file():
                continue
            if private and (media_dir / name).is_file():
                legacy += 1
                continue
            missing.append(f"{model._meta.label_lower}.{field.name}: {name}")
    return {
        "checked": checked,
        "legacy_public": legacy,
        "missing_count": len(missing),
        "missing_sample": missing[:MISSING_FILE_SAMPLE],
    }


def _verify_staged(alias: str, manifest: dict, targets: StageTargets) -> dict:
    result = {"errors": []}
    applied = migration_state(using=alias)
    if applied["applied"] != manifest["migration_state"]:
        result["errors"].append("применённые миграции staging-базы не совпадают с manifest")
    result["migration_fingerprint"] = applied["fingerprint"]
    try:
        marker = business_state_marker(using=alias)
    except Exception as exc:  # noqa: BLE001 — schema drift must be reported, not crash
        result["errors"].append(f"отпечаток данных не вычисляется: {type(exc).__name__}: {exc}")
    else:
        result["business_sha256"] = marker["business_sha256"]
        if marker != manifest["data_state"]:
            result["errors"].append("отпечаток бизнес-данных staging-базы не совпадает с manifest")
    result["files"] = check_file_references(alias, targets.media_dir, targets.private_media_dir)
    return result


# --- Entry points -------------------------------------------------------------


def stage_restore(
    run_dir,
    targets: StageTargets,
    *,
    private_media: ExternalPrivateMedia | None = None,
    settings_dict: dict | None = None,
) -> dict:
    """Restore ``run_dir`` into new staging slots and prove them; never cut over."""
    run_dir = Path(run_dir)
    s = dict(settings_dict or connection.settings_dict)
    _check_targets(run_dir, targets, s)
    with dr_operation_lock(s):
        return _stage_restore_locked(run_dir, targets, private_media, s)


def _stage_restore_locked(run_dir, targets, private_media, s) -> dict:
    manifest = _verify_inputs(run_dir, private_media)

    journal = Journal(targets.journal)
    journal.record(
        "verified",
        run_dir=str(run_dir.resolve()),
        backup_run_id=manifest["backup_run_id"],
        created_at=manifest["created_at"],
        app_commit=manifest["app_commit"],
        signature_key_id=manifest["signature"]["key_id"],
        external_private_media=private_media is not None,
    )
    stage = "creating_database"
    try:
        journal.record(stage, database=targets.database)
        oid = _create_database(s, targets.database)
        journal.record("database_created", database=targets.database, oid=oid)

        stage = "restoring_database"
        warnings = backup.restore_db(
            run_dir / manifest["database_dump_filename"],
            settings_dict={**s, "NAME": targets.database},
        )
        journal.record("database_restored", warnings=warnings)

        stage = "restoring_media"
        journal.record(stage, path=str(targets.media_dir))
        targets.media_dir.mkdir(mode=0o700)
        journal.record("media_dir_created", path=str(targets.media_dir))
        if manifest.get("media_filename"):
            backup.restore_media(run_dir / manifest["media_filename"], media_root=targets.media_dir)
        media_hash = media_tree_sha256(targets.media_dir)
        if media_hash != manifest["media_tree_sha256"]:
            raise StageRestoreError("Распакованные media не совпадают с manifest.")
        journal.record("media_restored", tree_sha256=media_hash)

        stage = "restoring_private_media"
        if private_media is None:
            archive = run_dir / manifest["private_media_filename"]
            expected = Inventory(
                manifest["private_media_file_count"],
                manifest["private_media_bytes"],
                manifest["private_media_tree_sha256"],
            )
        else:
            archive, expected = private_media.archive, private_media.expected
        journal.record(stage, path=str(targets.private_media_dir))
        targets.private_media_dir.mkdir(mode=0o700)
        journal.record("private_media_dir_created", path=str(targets.private_media_dir))
        packed = restore_archive(archive, targets.private_media_dir, expected=expected)
        journal.record(
            "private_media_restored", files=packed.files, bytes=packed.bytes,
            tree_sha256=packed.tree_sha256,
        )

        stage = "verifying_stage"
        with _StagedAlias(s, targets.database) as alias:
            proof = _verify_staged(alias, manifest, targets)
        if proof["errors"]:
            raise StageRestoreError("; ".join(proof["errors"]))
    except Exception as exc:
        journal.record("failed", failed_stage=stage, error=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, StageRestoreError):
            raise
        raise StageRestoreError(f"{stage}: {exc}") from exc

    verdict = "staged" if not proof["files"]["missing_count"] else "staged_needs_review"
    journal.record(
        verdict,
        migration_fingerprint=proof["migration_fingerprint"],
        business_sha256=proof["business_sha256"],
        files=proof["files"],
        cutover="not performed: this tool never switches the application",
    )
    return {"verdict": verdict, "manifest": manifest, "proof": proof}


def discard_stage(journal_path, *, settings_dict: dict | None = None) -> list[str]:
    """Remove only the slots the journal records as created by stage_restore."""
    s = dict(settings_dict or connection.settings_dict)
    rows = read_journal(journal_path)
    _refuse_discard_during_cutover(journal_path)
    creates_database = any(row["stage"] == "database_created" for row in rows)
    with dr_operation_lock(s, database=creates_database):
        return _discard_stage_locked(journal_path, s)


def _refuse_discard_during_cutover(journal_path) -> None:
    cutover = Path(str(journal_path) + CUTOVER_JOURNAL_SUFFIX)
    if not cutover.exists():
        return
    stages = [row["stage"] for row in read_journal(cutover) if row["stage"] != TORN_TAIL_STAGE]
    if not stages or stages[-1] != "rolled_back":
        raise StageRestoreError(
            "По этому staging начато переключение, и оно не откатено: удалять "
            "staging нельзя. См. dr_cutover_status."
        )


def _discard_stage_locked(journal_path, s: dict) -> list[str]:
    rows = read_journal(journal_path)
    if any(row["stage"] == "discarded" for row in rows):
        raise StageRestoreError("Этот staging уже удалён.")
    removed = []
    for row in rows:
        # Only a database this tool actually created; an intent row alone may
        # mean CREATE DATABASE failed on a name that belongs to someone else.
        if row["stage"] == "database_created":
            name = row["database"]
            if name == s.get("NAME") or not DATABASE_NAME_RE.fullmatch(name):
                raise StageRestoreError("Журнал указывает на рабочую или некорректную базу.")
            if row.get("oid") is not None:
                current = database_oid(s, name)
                if current is not None and current != row["oid"]:
                    raise StageRestoreError(
                        f"База {name} уже не та, что создал staging (другой oid): не удаляю."
                    )
            _drop_database(s, name)
            removed.append(f"database:{name}")
    live = [Path(settings.MEDIA_ROOT).resolve(), Path(settings.PRIVATE_MEDIA_ROOT).resolve()]
    for row in rows:
        if row["stage"] in {"media_dir_created", "private_media_dir_created"}:
            path = Path(row["path"]).resolve()
            if any(_overlaps(path, root) for root in live):
                raise StageRestoreError("Журнал указывает на рабочий каталог media.")
            if path.exists():
                shutil.rmtree(path)
            removed.append(f"dir:{path}")
    ExistingJournal(journal_path).record("discarded", removed=removed)
    return removed


class ExistingJournal(Journal):
    """Reopen for append, never recreate; seal a torn tail first."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_file():
            raise StageRestoreError(f"Журнал не найден: {self.path}")
        if journal_has_torn_tail(self.path):
            size = self.path.stat().st_size
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, b"\n")
                durable_fsync(fd)
            finally:
                os.close(fd)
            self.record(TORN_TAIL_STAGE, file_size_before=size)
