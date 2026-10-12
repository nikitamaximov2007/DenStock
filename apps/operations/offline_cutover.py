"""Offline DR, Round 4 step 2: switch the application to verified staged slots.

State machine (one journal per staged restore, ``<stage journal>.cutover``)::

    SOURCE_VERIFIED -> STAGED                     (offline_restore.stage_restore)
    -> VALIDATED -> WRITERS_QUIESCED -> CUTOVER_PREPARED        (prepare_cutover)
    -> CUTOVER (database, public media, private media switched)
    -> POST_CUTOVER_VERIFIED                                    (execute_cutover)
    -> COMMITTED                                                (commit_cutover)
    plus ROLLED_BACK and RECOVERY_REQUIRED                      (recover_cutover)

What is and is not atomic (do not overstate it):

* The database switch is ONE PostgreSQL transaction: ``ALTER DATABASE live
  RENAME TO old; ALTER DATABASE staged RENAME TO live``.  Either both renames
  commit or neither does.
* Each file store is switched by two ``rename(2)`` calls on one filesystem
  (live -> old, staged -> live).  A crash between them leaves the live path
  missing, which every process notices; each call itself is atomic.
* There is NO atomicity across PostgreSQL and the two file stores.  Instead the
  guarantee is: after any interruption the application cannot silently start
  with a database and file trees of different generations.  Before the first
  switch both databases get a generation marker in their PostgreSQL comment
  (``cutover_guard``); every process refuses a database whose marker is not a
  verified/committed new generation, and refuses a new generation whose media
  roots are not the exact directories (inodes) recorded for it.

Recovery never guesses: ``recover_cutover`` re-reads the journal, then probes
reality (which database OID carries which name, which directory inode sits at
which path).  Each slot must be exactly ``old``, ``half`` or ``new``; anything
else is RECOVERY_REQUIRED and nothing is touched.  Otherwise it rolls forward
(only when prepare completed and every staged slot still exists, and then the
new generation is re-proved before it may serve) or back.  A started rollback
is always finished as a rollback, never turned around.

Nothing here is reachable from HTTP: management commands only.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from pathlib import Path

import psycopg
from django.conf import settings
from django.db import DatabaseError, connection, connections
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

from .cutover_guard import (
    MARKER_KEY,
    MARKER_VERSION,
    guard_bypass,
    marker_problems,
    parse_marker,
    runtime_problems,
)
from .dr_faults import fault_point
from .emergency_manifest import validate_manifest
from .emergency_state import media_tree_sha256, sha256_file
from .manifest_signing import ManifestSignatureError, verify_manifest
from .models import DeploymentState
from .offline_restore import (
    CUTOVER_JOURNAL_SUFFIX,
    ExistingJournal,
    Journal,
    JournalCorruptError,
    StageRestoreError,
    StageTargets,
    _admin_params,
    _StagedAlias,
    _verify_staged,
    dr_operation_lock,
    lock_holder,
    read_journal,
    sync_directory,
)
from .private_media import Inventory, PrivateMediaError, inspect_tree

COMMIT_PHRASE = "ЗАФИКСИРОВАТЬ-DR"
SLOTS = ("database", "media", "private_media")
FILE_SLOTS = ("media", "private_media")
SETTING_FOR_SLOT = {"media": "MEDIA_ROOT", "private_media": "PRIVATE_MEDIA_ROOT"}
QUIESCE_WAIT_SECONDS = 5.0

STAGE_STATES = {
    "verified": "SOURCE_VERIFIED",
    "staged": "STAGED",
    "staged_needs_review": "STAGED_NEEDS_REVIEW",
    "failed": "STAGE_FAILED",
    "discarded": "STAGE_DISCARDED",
}
CUTOVER_STATES = {
    "validated": "VALIDATED",
    "writers_quiesced": "WRITERS_QUIESCED",
    "preparing": "CUTOVER_PREPARING",
    "prepared": "CUTOVER_PREPARED",
    "switching_database": "CUTOVER_IN_PROGRESS",
    "database_switched": "CUTOVER_IN_PROGRESS",
    "switching_media": "CUTOVER_IN_PROGRESS",
    "media_switched": "CUTOVER_IN_PROGRESS",
    "switching_private_media": "CUTOVER_IN_PROGRESS",
    "private_media_switched": "CUTOVER_IN_PROGRESS",
    "cutover_switched": "CUTOVER",
    "verifying": "CUTOVER",
    "post_cutover_verified": "POST_CUTOVER_VERIFIED",
    "switch_failed": "ROLLBACK_REQUIRED",
    "post_cutover_failed": "ROLLBACK_REQUIRED",
    "rollback_started": "ROLLBACK_IN_PROGRESS",
    "rollback_private_media": "ROLLBACK_IN_PROGRESS",
    "private_media_rolled_back": "ROLLBACK_IN_PROGRESS",
    "rollback_media": "ROLLBACK_IN_PROGRESS",
    "media_rolled_back": "ROLLBACK_IN_PROGRESS",
    "rollback_database": "ROLLBACK_IN_PROGRESS",
    "database_rolled_back": "ROLLBACK_IN_PROGRESS",
    "rolled_back": "ROLLED_BACK",
    "committing": "COMMITTING",
    "committed": "COMMITTED",
    "recovery_required": "RECOVERY_REQUIRED",
}
# States in which the configured runtime may serve, and the reality each needs.
OLD_GENERATION_STATES = frozenset({None, "VALIDATED", "WRITERS_QUIESCED", "ROLLED_BACK"})
NEW_GENERATION_STATES = frozenset({"POST_CUTOVER_VERIFIED", "COMMITTED"})


class CutoverError(StageRestoreError):
    pass


# --- Journal state -------------------------------------------------------------


def cutover_journal_path(stage_journal) -> Path:
    return Path(str(stage_journal) + CUTOVER_JOURNAL_SUFFIX)


def stage_state(rows: list[dict]) -> str | None:
    if not rows:
        return None
    last = rows[-1]["stage"]
    if last in STAGE_STATES:
        return STAGE_STATES[last]
    return "STAGING_INCOMPLETE"


def cutover_state(rows: list[dict]) -> str | None:
    state = None
    for row in rows:
        state = CUTOVER_STATES.get(row["stage"], state)
    return state


def _read(path) -> list[dict]:
    try:
        return read_journal(path)
    except JournalCorruptError as exc:
        raise CutoverError(
            f"{exc} Автоматическое восстановление запрещено: разберите журнал {path} "
            "вручную (см. restore-runbook.md)."
        ) from exc


def _plan(rows: list[dict]) -> dict | None:
    for row in rows:
        if row["stage"] == "preparing":
            return row["plan"]
    return None


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


# --- Staged slots from the staging journal -----------------------------------


def _stage_info(stage_journal: Path) -> dict:
    rows = _read(stage_journal)
    state = stage_state(rows)
    if state != "STAGED":
        raise CutoverError(
            f"Staging не готов к переключению: состояние {state}. Нужен итог staged "
            "(staged_needs_review, сбой или удалённый staging не переключаются)."
        )
    by_stage = {}
    for row in rows:
        by_stage.setdefault(row["stage"], row)
    verified = by_stage["verified"]
    if not verified.get("run_dir"):
        raise CutoverError("Журнал staging не содержит каталога бэкапа: повторите staging.")
    private = by_stage["private_media_restored"]
    return {
        "run_dir": verified["run_dir"],
        "backup_run_id": verified["backup_run_id"],
        "database": by_stage["database_created"]["database"],
        "database_oid": by_stage["database_created"].get("oid"),
        "media_dir": by_stage["media_dir_created"]["path"],
        "private_media_dir": by_stage["private_media_dir_created"]["path"],
        "private_inventory": [private["files"], private["bytes"], private["tree_sha256"]],
        "business_sha256": by_stage["staged"]["business_sha256"],
    }


def _load_manifest(run_dir, backup_run_id: str) -> dict:
    validation = validate_manifest(Path(run_dir), expected_source="production")
    if not validation.ok:
        raise CutoverError("Manifest не прошёл проверку: " + "; ".join(validation.errors))
    manifest = validation.manifest
    try:
        verify_manifest(manifest)
    except ManifestSignatureError as exc:
        raise CutoverError(f"Подпись manifest: {exc}") from exc
    if manifest["backup_run_id"] != backup_run_id:
        raise CutoverError("Manifest принадлежит другому бэкапу, чем staging.")
    return manifest


# --- PostgreSQL helpers --------------------------------------------------------


def _admin(s: dict):
    return psycopg.connect(**_admin_params(s))


def _find_databases(s: dict, names) -> dict[str, int]:
    with _admin(s) as admin:
        rows = admin.execute(
            "SELECT datname, oid FROM pg_database WHERE datname = ANY(%s)", (list(names),)
        ).fetchall()
    return {name: oid for name, oid in rows}


def _database_comment(s: dict, name: str):
    with _admin(s) as admin:
        row = admin.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
            (name,),
        ).fetchone()
    return row[0] if row else None


def _comment_statement(name: str, raw: str | None):
    value = sql.NULL if raw is None else sql.Literal(raw)
    return sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), value)


def _marker(plan: dict, role: str, state: str) -> str:
    marker = {
        MARKER_KEY: MARKER_VERSION,
        "cutover_id": plan["cutover_id"],
        "role": role,
        "state": state,
    }
    if role == "new":
        marker.update(
            generation=plan["generation"],
            backup_run_id=plan["backup_run_id"],
            media_ino=plan["media"]["staged_ino"],
            private_media_ino=plan["private_media"]["staged_ino"],
        )
    else:
        marker["generation"] = plan["old_generation"]
    return json.dumps(marker, sort_keys=True)


class _Alias(_StagedAlias):
    """A guard-free Django alias to one database, closed on exit."""


def _pending_migrations(alias: str) -> list[str]:
    executor = MigrationExecutor(connections[alias])
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan]


def _write_state(s: dict, name: str) -> str:
    with _Alias(s, name) as alias:
        return (
            DeploymentState.objects.using(alias)
            .values_list("write_state", flat=True)
            .get(pk=DeploymentState.SINGLETON_PK)
        )


# --- Reality probe ---------------------------------------------------------------


def _classify(live, staged, old, live_id, staged_id) -> str:
    """Which generation a slot holds, from identities found at its three names."""
    if live == live_id and old is None:
        if staged == staged_id:
            return "old"
        return "old-no-stage" if staged is None else "unknown"
    if live == staged_id and old == live_id and staged is None:
        return "new"
    if live is None and old == live_id and staged == staged_id:
        return "half"
    return "unknown"


def _dir_identity(path) -> int | None:
    """Inode of a real directory; -1 for anything else at that path."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return -1
    return st.st_ino


def probe(plan: dict, s: dict) -> dict:
    db = plan["database"]
    found = _find_databases(s, [db["live"], db["staged"], db["old"]])
    reality = {
        "database": {
            "state": _classify(
                found.get(db["live"]), found.get(db["staged"]), found.get(db["old"]),
                db["live_oid"], db["staged_oid"],
            ),
            "found": found,
        }
    }
    for slot in FILE_SLOTS:
        paths = plan[slot]
        ids = {key: _dir_identity(paths[key]) for key in ("live", "staged", "old")}
        reality[slot] = {
            "state": _classify(
                ids["live"], ids["staged"], ids["old"], paths["live_ino"], paths["staged_ino"]
            ),
            "inodes": ids,
        }
    return reality


def _states(reality: dict) -> dict:
    return {slot: reality[slot]["state"] for slot in SLOTS}


# --- Writers quiesced --------------------------------------------------------------


def _assert_quiesced(s: dict, plan: dict, writers_stopped: bool) -> dict:
    if not writers_stopped:
        raise CutoverError(
            "Подтвердите, что web, catalog-web, telegram-bot, max-bot и proxy "
            "остановлены: флаг --writers-stopped."
        )
    connections.close_all()
    db = plan["database"]
    found = _find_databases(s, [db["live"], db["staged"], db["old"]])
    by_oid = {oid: name for name, oid in found.items()}
    old_name = by_oid.get(db["live_oid"])
    if old_name is None:
        raise CutoverError("Рабочая (старая) база не найдена по её oid.")
    write_state = _write_state(s, old_name)
    if write_state != DeploymentState.WriteState.MAINTENANCE:
        raise CutoverError(
            f"Рабочая база в состоянии записи {write_state}: сначала включите режим "
            "только для чтения (production_maintenance --enable)."
        )
    deadline = time.monotonic() + QUIESCE_WAIT_SECONDS
    while True:  # our own just-closed sessions take a moment to leave
        with _admin(s) as admin:
            others = admin.execute(
                "SELECT pid, datname, usename, application_name FROM pg_stat_activity "
                "WHERE datname = ANY(%s) AND pid <> pg_backend_pid()",
                (list(found),),
            ).fetchall()
        if not others or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    if others:
        listed = ", ".join(f"{d}:{u}:{a or '-'}:{p}" for p, d, u, a in others[:10])
        raise CutoverError(
            f"К базам подключены другие процессы ({len(others)}): {listed}. "
            "Остановите их и повторите."
        )
    return {"write_state": write_state, "other_connections": 0, "operator_flag": True}


# --- Proofs --------------------------------------------------------------------------


def _prove(s: dict, database: str, media_dir, private_dir, plan_or_info: dict,
           manifest: dict) -> dict:
    """Prove one database + two directories against the signed manifest."""
    errors = []
    with _Alias(s, database) as alias:
        proof = _verify_staged(
            alias, manifest,
            StageTargets(database, Path(media_dir), Path(private_dir), Path(os.devnull)),
        )
        pending = _pending_migrations(alias)
    errors.extend(proof["errors"])
    if pending:
        errors.append(
            "код ожидает миграции, которых нет в базе: " + ", ".join(pending[:5])
        )
    if proof["files"]["missing_count"]:
        errors.append(
            "база ссылается на отсутствующие файлы: "
            + "; ".join(proof["files"]["missing_sample"][:5])
        )
    media_hash = media_tree_sha256(media_dir)
    if media_hash != manifest["media_tree_sha256"]:
        errors.append("дерево media не совпадает с manifest")
    expected = Inventory(*plan_or_info["private_inventory"])
    try:
        private = inspect_tree(private_dir, require_present=True)
    except PrivateMediaError as exc:
        private = None
        errors.append(f"private_media: {exc}")
    if private is not None and private != expected:
        errors.append("дерево private_media не совпадает с инвентарём staging")
    business = proof.get("business_sha256")
    if business and business != plan_or_info["business_sha256"]:
        errors.append("отпечаток бизнес-данных не совпадает с итогом staging")
    return {
        "errors": errors,
        "business_sha256": business,
        "migration_fingerprint": proof.get("migration_fingerprint"),
        "media_tree_sha256": media_hash,
        "private_media": (
            [private.files, private.bytes, private.tree_sha256] if private else None
        ),
        "files_checked": proof["files"]["checked"],
    }


# --- Prepare -------------------------------------------------------------------------


def _live_root(slot: str) -> Path:
    return Path(os.path.abspath(getattr(settings, SETTING_FOR_SLOT[slot])))


def _file_slot_plan(slot: str, staged_path: str, cutover_id: str) -> dict:
    live = _live_root(slot)
    staged = Path(staged_path)
    old = live.with_name(f"{live.name}.dr-old-{cutover_id[:12]}")
    for label, path in (("рабочий", live), ("staging", staged)):
        if path.is_symlink() or not path.is_dir():
            raise CutoverError(f"{slot}: {label} каталог не найден или не каталог: {path}")
    if os.path.ismount(live):
        raise CutoverError(
            f"{slot}: {live} является точкой монтирования, его нельзя переименовать. "
            "Запускайте переключение там, где смонтирован родительский каталог тома "
            "(см. restore-runbook.md)."
        )
    if os.stat(live).st_dev != os.stat(staged).st_dev:
        raise CutoverError(
            f"{slot}: staging-каталог {staged} на другой файловой системе, чем {live}."
        )
    if old.exists() or old.is_symlink():
        raise CutoverError(f"{slot}: место для старого поколения уже занято: {old}")
    if not os.access(live.parent, os.W_OK):
        raise CutoverError(f"{slot}: нет права записи в {live.parent}")
    return {
        "live": str(live),
        "staged": str(staged),
        "old": str(old),
        "live_ino": os.lstat(live).st_ino,
        "staged_ino": os.lstat(staged).st_ino,
    }


def _old_database_name(live: str, cutover_id: str) -> str:
    return f"{live[:50]}_dro_{cutover_id[:8]}"


def _build_plan(s: dict, info: dict, manifest: dict, stage_journal: Path) -> dict:
    live, staged = str(s["NAME"]), info["database"]
    if live == staged:
        raise CutoverError("Staging-база совпадает с рабочей.")
    cutover_id = uuid.uuid4().hex
    old = _old_database_name(live, cutover_id)
    found = _find_databases(s, [live, staged, old])
    if live not in found:
        raise CutoverError(f"Рабочая база {live} не найдена.")
    if staged not in found:
        raise CutoverError(f"Staging-база {staged} не найдена.")
    if info["database_oid"] is not None and found[staged] != info["database_oid"]:
        raise CutoverError(f"Staging-база {staged} подменена (другой oid).")
    if old in found:
        raise CutoverError(f"Имя для старой базы уже занято: {old}")
    previous = _database_comment(s, live)
    previous_marker = parse_marker(previous)
    if previous_marker is not None and previous_marker.get("state") != "committed":
        raise CutoverError("Рабочая база уже участвует в незавершённом DR-переключении.")
    if parse_marker(_database_comment(s, staged)) is not None:
        raise CutoverError("Staging-база уже помечена другим переключением.")
    return {
        "cutover_id": cutover_id,
        "generation": str(uuid.uuid4()),
        "old_generation": f"before-{cutover_id}",
        "backup_run_id": manifest["backup_run_id"],
        "database_identity": manifest["data_state"]["database_identity"],
        "media_tree_sha256": manifest["media_tree_sha256"],
        "private_inventory": info["private_inventory"],
        "business_sha256": info["business_sha256"],
        "run_dir": info["run_dir"],
        "stage_journal": str(stage_journal),
        "previous_live_comment": previous,
        "database": {
            "live": live, "staged": staged, "old": old,
            "live_oid": found[live], "staged_oid": found[staged],
        },
        "media": _file_slot_plan("media", info["media_dir"], cutover_id),
        "private_media": _file_slot_plan("private_media", info["private_media_dir"], cutover_id),
    }


def prepare_cutover(stage_journal, *, writers_stopped: bool, settings_dict=None) -> dict:
    """VALIDATED -> WRITERS_QUIESCED -> CUTOVER_PREPARED; switches nothing yet.

    Every check runs before the cutover journal exists, so a refusal can simply
    be retried.  The journal is created only right before the markers are
    stamped; from then on every process refuses both databases.
    """
    s = dict(settings_dict or connection.settings_dict)
    if "postgresql" not in s["ENGINE"]:
        raise CutoverError("Переключение поддерживает только PostgreSQL.")
    if not writers_stopped:
        raise CutoverError(
            "Подтвердите, что web, catalog-web, telegram-bot, max-bot и proxy "
            "остановлены: флаг --writers-stopped."
        )
    stage_journal = Path(stage_journal)
    with dr_operation_lock(s), guard_bypass():
        journal_path = cutover_journal_path(stage_journal)
        if journal_path.exists():
            raise CutoverError(
                f"Переключение по этому staging уже начато ({journal_path}): "
                "см. dr_cutover_status и dr_cutover_recover."
            )
        info = _stage_info(stage_journal)
        manifest = _load_manifest(info["run_dir"], info["backup_run_id"])
        plan = _build_plan(s, info, manifest, stage_journal)
        proof = _prove(
            s, info["database"], info["media_dir"], info["private_media_dir"], info, manifest
        )
        if proof["errors"]:
            raise CutoverError(
                "Staging не прошёл повторную проверку: " + "; ".join(proof["errors"])
            )
        quiesce = _assert_quiesced(s, plan, writers_stopped)

        journal = Journal(journal_path)
        journal.record("validated", proof=proof)
        journal.record("writers_quiesced", **quiesce)
        journal.record("preparing", plan=plan)
        with _Alias(s, plan["database"]["staged"]) as alias:
            DeploymentState.objects.using(alias).filter(
                pk=DeploymentState.SINGLETON_PK
            ).update(
                write_state=DeploymentState.WriteState.MAINTENANCE,
                state_reason=f"DR cutover {plan['cutover_id']}"[:255],
            )
        connections.close_all()
        fault_point("cutover:before_markers")
        with _admin(s) as admin, admin.transaction():
            admin.execute(_comment_statement(
                plan["database"]["staged"], _marker(plan, "new", "cutover")
            ))
            admin.execute(_comment_statement(
                plan["database"]["live"], _marker(plan, "old", "cutover")
            ))
        journal.record("prepared", cutover_id=plan["cutover_id"])
    return {"plan": plan, "proof": proof, "journal": str(journal_path)}


# --- Switching -----------------------------------------------------------------------


def _switch_database(s: dict, plan: dict, *, forward: bool) -> None:
    db = plan["database"]
    with _admin(s) as admin, admin.transaction():
        found = dict(admin.execute(
            "SELECT datname, oid FROM pg_database WHERE datname = ANY(%s)",
            ([db["live"], db["staged"], db["old"]],),
        ).fetchall())
        state = _classify(
            found.get(db["live"]), found.get(db["staged"]), found.get(db["old"]),
            db["live_oid"], db["staged_oid"],
        )
        if forward:
            renames = {
                "old": [(db["live"], db["old"]), (db["staged"], db["live"])],
                "half": [(db["staged"], db["live"])],
                "new": [],
            }.get(state)
        else:
            renames = {
                "new": [(db["live"], db["staged"]), (db["old"], db["live"])],
                "half": [(db["old"], db["live"])],
                "old": [],
                "old-no-stage": [],
            }.get(state)
        if renames is None:
            raise CutoverError(f"База данных в неожиданном состоянии {state}: {found}")
        for source, target in renames:
            admin.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                sql.Identifier(source), sql.Identifier(target)
            ))


def _rename(source: Path, target: Path) -> None:
    os.rename(source, target)
    sync_directory(target.parent)
    if source.parent != target.parent:
        sync_directory(source.parent)


def _switch_dir(slot: str, paths: dict, *, forward: bool) -> None:
    live, staged, old = (Path(paths[key]) for key in ("live", "staged", "old"))

    def state():
        return _classify(
            _dir_identity(live), _dir_identity(staged), _dir_identity(old),
            paths["live_ino"], paths["staged_ino"],
        )

    current = state()
    if forward:
        if current == "old":
            _rename(live, old)
            fault_point(f"cutover:{slot}_half")
            current = "half"
        if current == "half":
            _rename(staged, live)
            return
        if current == "new":
            return
    else:
        if current in ("old", "old-no-stage"):
            return
        if current == "new":
            _rename(live, staged)
            fault_point(f"rollback:{slot}_half")
            current = "half"
        if current == "half":
            _rename(old, live)
            return
    raise CutoverError(f"{slot}: каталоги в неожиданном состоянии {current}.")


def _forward(journal: Journal, plan: dict, manifest: dict, s: dict) -> dict:
    reality = probe(plan, s)
    try:
        for slot in SLOTS:
            current = reality[slot]["state"]
            if current == "new":
                continue
            if current not in ("old", "half"):
                raise CutoverError(f"{slot}: состояние {current}, вперёд нельзя")
            journal.record(f"switching_{slot}")
            fault_point(f"cutover:before_{slot}_switch")
            if slot == "database":
                _switch_database(s, plan, forward=True)
            else:
                _switch_dir(slot, plan[slot], forward=True)
            journal.record(f"{slot}_switched")
            fault_point(f"cutover:after_{slot}_switch")
        journal.record("cutover_switched")
    except (OSError, psycopg.Error, CutoverError) as exc:
        journal.record("switch_failed", error=_err(exc))
        _rollback(journal, plan, s, reason=f"переключение не удалось: {_err(exc)}")
        raise CutoverError(
            f"Переключение не удалось, старое поколение возвращено: {_err(exc)}"
        ) from exc
    return _post_verify(journal, plan, manifest, s)


def _post_verify(journal: Journal, plan: dict, manifest: dict, s: dict) -> dict:
    journal.record("verifying")
    errors = []
    reality = probe(plan, s)
    if any(state != "new" for state in _states(reality).values()):
        errors.append(f"не все хранилища переключены: {_states(reality)}")
        proof = None
    else:
        db = plan["database"]
        proof = _prove(
            s, db["live"], plan["media"]["live"], plan["private_media"]["live"], plan, manifest
        )
        errors.extend(proof["errors"])
        marker = parse_marker(_database_comment(s, db["live"]))
        if (
            not marker or marker.get("cutover_id") != plan["cutover_id"]
            or marker.get("role") != "new"
        ):
            errors.append("рабочая база не несёт метку нового поколения")
    if not errors:
        verified = _marker(plan, "new", "verified")
        problems = marker_problems(parse_marker(verified))
        if problems:
            errors.extend(problems)
    if errors:
        journal.record("post_cutover_failed", errors=errors)
        _rollback(journal, plan, s, reason="проверка после переключения: " + "; ".join(errors))
        raise CutoverError(
            "Проверка после переключения не прошла, старое поколение возвращено: "
            + "; ".join(errors)
        )
    fault_point("cutover:before_verified_marker")
    with _admin(s) as admin:
        admin.execute(_comment_statement(plan["database"]["live"], verified))
    journal.record("post_cutover_verified", proof=proof)
    return {"action": "forward", "state": "POST_CUTOVER_VERIFIED", "proof": proof}


# --- Rollback ------------------------------------------------------------------------


def _restore_markers(s: dict, plan: dict) -> None:
    """After a rollback: old generation as before, staged slot marked rolled back."""
    db = plan["database"]
    with _admin(s) as admin, admin.transaction():
        found = dict(admin.execute(
            "SELECT datname, oid FROM pg_database WHERE datname = ANY(%s)",
            ([db["live"], db["staged"]],),
        ).fetchall())
        if found.get(db["live"]) != db["live_oid"]:
            raise CutoverError("После отката под рабочим именем не старая база.")
        admin.execute(_comment_statement(db["live"], plan["previous_live_comment"]))
        if found.get(db["staged"]) == db["staged_oid"]:
            admin.execute(_comment_statement(db["staged"], _marker(plan, "new", "rolled_back")))


def _rollback(journal: Journal, plan: dict, s: dict, *, reason: str) -> dict:
    if cutover_state(read_journal(journal.path)) != "ROLLBACK_IN_PROGRESS":
        journal.record("rollback_started", reason=reason)
    reality = probe(plan, s)
    unknown = [slot for slot, state in _states(reality).items() if state == "unknown"]
    if unknown:
        journal.record("recovery_required", reason=f"неизвестное состояние: {unknown}",
                       reality=reality)
        raise CutoverError(
            f"Откат невозможен без ручного разбора: {', '.join(unknown)} в неизвестном "
            "состоянии. Ничего не изменено; приложение не запустится."
        )
    for slot in ("private_media", "media", "database"):
        if reality[slot]["state"] in ("old", "old-no-stage"):
            continue
        journal.record(f"rollback_{slot}")
        if slot == "database":
            _switch_database(s, plan, forward=False)
        else:
            _switch_dir(slot, plan[slot], forward=False)
        journal.record(f"{slot}_rolled_back")
        fault_point(f"rollback:after_{slot}")
    _restore_markers(s, plan)
    journal.record("rolled_back", reason=reason)
    return {"action": "rolled_back", "state": "ROLLED_BACK", "reason": reason}


# --- Entry points ----------------------------------------------------------------------


def _locked_journal(stage_journal) -> tuple[Path, list[dict], str | None]:
    path = cutover_journal_path(stage_journal)
    if not path.exists():
        raise CutoverError(f"Журнал переключения не найден: {path}")
    rows = _read(path)
    return path, rows, cutover_state(rows)


def execute_cutover(stage_journal, *, writers_stopped: bool, settings_dict=None) -> dict:
    """CUTOVER_PREPARED -> CUTOVER -> POST_CUTOVER_VERIFIED (or ROLLED_BACK)."""
    s = dict(settings_dict or connection.settings_dict)
    with dr_operation_lock(s), guard_bypass():
        path, rows, state = _locked_journal(stage_journal)
        if state != "CUTOVER_PREPARED":
            raise CutoverError(
                f"Ожидалось CUTOVER_PREPARED, сейчас {state}: используйте dr_cutover_recover."
            )
        plan = _plan(rows)
        manifest = _load_manifest(plan["run_dir"], plan["backup_run_id"])
        states = _states(probe(plan, s))
        if any(value != "old" for value in states.values()):
            raise CutoverError(
                f"Хранилища не совпадают с журналом ({states}): используйте dr_cutover_recover."
            )
        _assert_quiesced(s, plan, writers_stopped)
        return _forward(ExistingJournal(path), plan, manifest, s)


def recover_cutover(stage_journal, *, writers_stopped: bool, rollback: bool = False,
                    settings_dict=None) -> dict:
    """Finish an interrupted cutover forward when safe, else roll it back."""
    s = dict(settings_dict or connection.settings_dict)
    with dr_operation_lock(s), guard_bypass():
        path = cutover_journal_path(stage_journal)
        if not path.exists():
            return {"action": "none", "state": None,
                    "reason": "переключение по этому staging не начиналось"}
        rows = _read(path)
        state = cutover_state(rows)
        if state in ("COMMITTED", "ROLLED_BACK"):
            return {"action": "none", "state": state, "reason": "переключение уже завершено"}
        if state == "RECOVERY_REQUIRED":
            raise CutoverError(
                "Журнал требует ручного восстановления (RECOVERY_REQUIRED): автоматически "
                "ничего не делаю. См. dr_cutover_status и restore-runbook.md."
            )
        if state == "POST_CUTOVER_VERIFIED" and not rollback:
            return {"action": "none", "state": state,
                    "reason": "новое поколение проверено, ждёт dr_cutover_commit"}
        if state == "COMMITTING":
            raise CutoverError("Фиксация прервана: повторите dr_cutover_commit.")
        plan = _plan(rows)
        journal = ExistingJournal(path)
        if plan is None:  # nothing was stamped or switched
            journal.record("rolled_back", reason="подготовка не дошла до меток, ничего не менялось")
            return {"action": "rolled_back", "state": "ROLLED_BACK",
                    "reason": "подготовка не дошла до меток"}
        reality = probe(plan, s)
        states = _states(reality)
        if "unknown" in states.values():
            journal.record("recovery_required", reason="неизвестное состояние хранилищ",
                           reality=reality)
            raise CutoverError(
                f"Фактическое состояние не совпадает ни со старым, ни с новым поколением "
                f"({states}). Ничего не изменено; нужен ручной разбор."
            )
        _assert_quiesced(s, plan, writers_stopped)
        journal.record("recover_started", state=state, reality=states,
                       requested="rollback" if rollback else "auto")
        backward_states = ("ROLLBACK_IN_PROGRESS", "ROLLBACK_REQUIRED", "CUTOVER_PREPARING")
        if rollback or state in backward_states:
            reason = "откат по запросу оператора" if rollback else f"продолжение отката ({state})"
            if state == "CUTOVER_PREPARING":
                reason = "подготовка прервана до завершения"
            return _rollback(journal, plan, s, reason=reason)
        if "old-no-stage" in states.values():
            return _rollback(journal, plan, s, reason="staging-слот пропал: вперёд нельзя")
        manifest = _load_manifest(plan["run_dir"], plan["backup_run_id"])
        return _forward(journal, plan, manifest, s)


def _verify_post_backup(run_dir, plan: dict) -> dict:
    run_dir = Path(run_dir)
    validation = validate_manifest(run_dir, expected_source="production")
    if not validation.ok:
        raise CutoverError("POST-бэкап не прошёл проверку: " + "; ".join(validation.errors))
    manifest = validation.manifest
    try:
        verify_manifest(manifest)
    except ManifestSignatureError as exc:
        raise CutoverError(f"Подпись POST-бэкапа: {exc}") from exc
    for name, expected in (manifest.get("sha256") or {}).items():
        path = run_dir / name
        if not path.is_file() or sha256_file(path) != expected:
            raise CutoverError(f"POST-бэкап: контрольная сумма {name} не совпадает.")
    if manifest["backup_run_id"] == plan["backup_run_id"]:
        raise CutoverError("Это исходный бэкап, а нужен новый POST-бэкап нового поколения.")
    problems = []
    if manifest["data_state"]["business_sha256"] != plan["business_sha256"]:
        problems.append("бизнес-данные изменились после проверки")
    if manifest["data_state"]["database_identity"] != plan["database_identity"]:
        problems.append("другая database_identity")
    if manifest["media_tree_sha256"] != plan["media_tree_sha256"]:
        problems.append("дерево media другое")
    private = plan["private_inventory"]
    if (
        manifest.get("private_media_status") != "included"
        or manifest.get("private_media_tree_sha256") != private[2]
    ):
        problems.append("private_media другое или отсутствует")
    if problems:
        raise CutoverError("POST-бэкап не описывает проверенное поколение: " + "; ".join(problems))
    return manifest


def commit_cutover(stage_journal, *, post_backup, confirm: str, settings_dict=None) -> dict:
    """POST_CUTOVER_VERIFIED -> COMMITTED once a POST backup proves the generation."""
    if confirm != COMMIT_PHRASE:
        raise CutoverError(f"Для фиксации укажите --confirm {COMMIT_PHRASE}")
    s = dict(settings_dict or connection.settings_dict)
    with dr_operation_lock(s), guard_bypass():
        path, rows, state = _locked_journal(stage_journal)
        if state not in ("POST_CUTOVER_VERIFIED", "COMMITTING"):
            raise CutoverError(f"Фиксация возможна только после проверки, сейчас {state}.")
        plan = _plan(rows)
        states = _states(probe(plan, s))
        if any(value != "new" for value in states.values()):
            raise CutoverError(f"Хранилища не в новом поколении ({states}): фиксация запрещена.")
        marker = parse_marker(_database_comment(s, plan["database"]["live"]))
        if not marker or marker.get("cutover_id") != plan["cutover_id"] or marker.get(
            "state"
        ) not in ("verified", "committed"):
            raise CutoverError("Метка рабочей базы не соответствует проверенному поколению.")
        post = _verify_post_backup(post_backup, plan)
        journal = ExistingJournal(path)
        if state != "COMMITTING":
            journal.record("committing", post_backup=str(post_backup),
                           post_backup_run_id=post["backup_run_id"])
        db = plan["database"]
        with _admin(s) as admin, admin.transaction():
            admin.execute(_comment_statement(db["live"], _marker(plan, "new", "committed")))
            admin.execute(_comment_statement(db["old"], _marker(plan, "old", "superseded")))
        retained = {
            "database": db["old"],
            "media": plan["media"]["old"],
            "private_media": plan["private_media"]["old"],
        }
        journal.record("committed", post_backup_run_id=post["backup_run_id"], retained=retained)
    return {"action": "committed", "state": "COMMITTED", "retained": retained,
            "post_backup_run_id": post["backup_run_id"]}


def cutover_status(stage_journal=None, *, settings_dict=None) -> dict:
    """Read-only: journal states, reality, and whether this runtime may serve."""
    s = dict(settings_dict or connection.settings_dict)
    report = {"lock_holder": lock_holder(), "problems": []}
    try:
        report["runtime_problems"] = runtime_problems()
    except DatabaseError as exc:
        report["runtime_problems"] = [f"база недоступна: {exc}"]
    report["problems"].extend(report["runtime_problems"])
    state = None
    if stage_journal is not None:
        stage_journal = Path(stage_journal)
        try:
            report["stage_state"] = stage_state(read_journal(stage_journal))
        except (JournalCorruptError, OSError) as exc:
            report["stage_state"] = "JOURNAL_UNREADABLE"
            report["problems"].append(f"журнал staging: {exc}")
        path = cutover_journal_path(stage_journal)
        if path.exists():
            try:
                rows = read_journal(path)
            except JournalCorruptError as exc:
                rows = None
                state = "JOURNAL_CORRUPT"
                report["problems"].append(f"журнал переключения повреждён: {exc}")
            if rows is not None:
                state = cutover_state(rows)
                plan = _plan(rows)
                if plan is not None:
                    report["cutover_id"] = plan["cutover_id"]
                    with guard_bypass():
                        reality = probe(plan, s)
                    report["reality"] = _states(reality)
                    report["reality_detail"] = reality
                    report["slots"] = {
                        slot: {
                            key: plan[slot][key]
                            for key in ("live", "staged", "old", "live_oid", "staged_oid",
                                        "live_ino", "staged_ino")
                            if key in plan[slot]
                        }
                        for slot in SLOTS
                    }
                    values = set(report["reality"].values())
                    if state in OLD_GENERATION_STATES and not values <= {"old", "old-no-stage"}:
                        report["problems"].append("журнал и хранилища расходятся")
                    if state in NEW_GENERATION_STATES and values != {"new"}:
                        report["problems"].append("журнал и хранилища расходятся")
        report["cutover_state"] = state
        if state not in OLD_GENERATION_STATES | NEW_GENERATION_STATES:
            report["problems"].append(f"переключение не завершено: {state}")
    report["safe_to_serve"] = not report["problems"]
    return report
