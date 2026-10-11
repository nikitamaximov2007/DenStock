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
slots stays a separate, still unimplemented operator step.  ``discard_stage``
removes only what the journal says this tool created.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
from django.apps import apps
from django.conf import settings
from django.db import connection, connections, models
from django.utils import timezone
from psycopg import sql

from . import backup
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


class StageRestoreError(Exception):
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


class Journal:
    """Append-only, fsynced JSON-lines record; created exclusively."""

    def __init__(self, path: Path):
        self.path = Path(path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        os.close(fd)
        self._sync_dir()

    def _sync_dir(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def record(self, stage: str, **data) -> None:
        line = json.dumps(
            {"at": timezone.now().isoformat(timespec="seconds"), "stage": stage, **data},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._sync_dir()


def read_journal(path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


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


def _create_database(s: dict, name: str) -> None:
    with psycopg.connect(**_admin_params(s)) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if exists:
            raise StageRestoreError(f"База {name} уже существует: staging только в новую базу.")
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


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
    manifest = _verify_inputs(run_dir, private_media)

    journal = Journal(targets.journal)
    journal.record(
        "verified",
        backup_run_id=manifest["backup_run_id"],
        created_at=manifest["created_at"],
        app_commit=manifest["app_commit"],
        signature_key_id=manifest["signature"]["key_id"],
        external_private_media=private_media is not None,
    )
    stage = "creating_database"
    try:
        journal.record(stage, database=targets.database)
        _create_database(s, targets.database)
        journal.record("database_created", database=targets.database)

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
    _ExistingJournal(journal_path).record("discarded", removed=removed)
    return removed


class _ExistingJournal(Journal):
    def __init__(self, path):  # reopen for append, never recreate
        self.path = Path(path)
