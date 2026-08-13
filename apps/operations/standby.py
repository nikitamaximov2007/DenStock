"""Verified, staged refresh of the local emergency standby."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import psycopg
from django.conf import settings
from django.db import connection
from psycopg import sql

from . import backup
from .emergency_environment import validate_database_target
from .emergency_manifest import validate_manifest
from .emergency_state import application_migration_state, media_tree_sha256, record_event
from .models import OfflineSession

CONTROL_SCHEMA_VERSION = 1
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_SLOT = re.compile(r"^[0-9a-f]{12}$")


class StandbyError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmergencyPaths:
    root: Path

    @property
    def control(self):
        return self.root / "control.json"

    @property
    def staging(self):
        return self.root / "staging"

    @property
    def standbys(self):
        return self.root / "standbys"

    @property
    def lock(self):
        return self.root / "control.lock"

    @classmethod
    def configured(cls):
        return cls(Path(settings.DENSTOCK_EMERGENCY_ROOT).resolve())


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, path)


def save_control(value: dict, paths=None) -> None:
    paths = paths or EmergencyPaths.configured()
    value = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "active_standby": value.get("active_standby"),
        "previous_standbys": value.get("previous_standbys", []),
        "offline_lifecycle": value.get("offline_lifecycle"),
    }
    _write_json_atomic(paths.control, value)


@contextmanager
def control_lock(paths=None):
    """Serialize standby and offline lifecycle changes across processes."""
    paths = paths or EmergencyPaths.configured()
    paths.root.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_control(paths=None) -> dict:
    paths = paths or EmergencyPaths.configured()
    if not paths.control.exists():
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "active_standby": None,
            "previous_standbys": [],
            "offline_lifecycle": None,
        }
    try:
        value = json.loads(paths.control.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StandbyError("Emergency control state повреждён.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise StandbyError("Emergency control state имеет неизвестную версию.")
    value.setdefault("active_standby", None)
    value.setdefault("previous_standbys", [])
    value.setdefault("offline_lifecycle", None)
    return value


def set_offline_lifecycle(session, *, status=None, paths=None, details=None) -> dict:
    """Persist credential-free lifecycle state while the caller holds control_lock."""
    paths = paths or EmergencyPaths.configured()
    control = load_control(paths)
    active = control.get("active_standby") or {}
    payload = {
        "session_id": str(session.id),
        "status": status or session.status,
        "database_name": active.get("database_name", ""),
        "base_backup_run_id": session.base_backup_run_id,
        "kind": session.kind,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if details:
        payload["details"] = details
    control["offline_lifecycle"] = payload
    save_control(control, paths)
    return payload


def active_standby_run_dir(active: dict, paths=None) -> Path:
    """Resolve a control entry only when every path is inside its recorded slot."""
    paths = paths or EmergencyPaths.configured()
    slot = str(active.get("slot", ""))
    database_name = str(active.get("database_name", ""))
    if not SAFE_SLOT.fullmatch(slot):
        raise StandbyError("Active standby slot имеет небезопасный формат.")
    if database_name != f"{settings.DENSTOCK_EMERGENCY_DB_PREFIX}{slot}":
        raise StandbyError("Active standby database не соответствует slot.")
    slot_dir = (paths.standbys / slot).resolve()
    if slot_dir.parent != paths.standbys.resolve():
        raise StandbyError("Active standby slot находится вне standby root.")
    manifest_path = Path(str(active.get("manifest_path", ""))).resolve()
    media_root = Path(str(active.get("media_root", ""))).resolve()
    if manifest_path != slot_dir / "manifest.json" or media_root != slot_dir / "media":
        raise StandbyError("Active standby paths не соответствуют slot.")
    if not manifest_path.is_file() or not media_root.is_dir():
        raise StandbyError("Active standby files отсутствуют.")
    return slot_dir


def _is_remote(source: str) -> bool:
    return ":" in source and not Path(source).drive


def _run_rclone(arguments: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["rclone", *arguments], capture_output=True, text=True, check=True
        )
    except FileNotFoundError as exc:
        raise StandbyError("rclone не установлен.") from exc
    except subprocess.CalledProcessError as exc:
        raise StandbyError(f"rclone завершился с ошибкой: {exc.stderr or exc.returncode}") from exc


def _latest_remote_run(source: str) -> str:
    result = _run_rclone(["lsf", source, "--dirs-only"])
    runs = sorted(
        (line.strip().rstrip("/") for line in result.stdout.splitlines()), reverse=True
    )
    if not runs or not SAFE_RUN_ID.fullmatch(runs[0]):
        raise StandbyError("В remote не найден безопасный backup run.")
    return runs[0]


def _latest_local_run(source: Path) -> str:
    if (source / "manifest.json").is_file():
        if not SAFE_RUN_ID.fullmatch(source.name):
            raise StandbyError("Некорректный backup run id.")
        return source.name
    runs = sorted(
        (
            path.name
            for path in source.iterdir()
            if path.is_dir() and SAFE_RUN_ID.fullmatch(path.name)
        ),
        reverse=True,
    )
    if not runs:
        raise StandbyError("В источнике не найден backup run.")
    return runs[0]


def fetch_backup(source: str, destination: Path, *, run_id=None) -> tuple[Path, str]:
    destination.mkdir(parents=True, exist_ok=False)
    if _is_remote(source):
        chosen = run_id or _latest_remote_run(source)
        if not SAFE_RUN_ID.fullmatch(chosen):
            raise StandbyError("Некорректный backup run id.")
        _run_rclone(["copy", f"{source.rstrip('/')}/{chosen}", str(destination)])
        return destination, chosen

    source_path = Path(source).resolve()
    if not source_path.exists():
        raise StandbyError("Локальный источник backup не найден.")
    chosen = run_id or _latest_local_run(source_path)
    selected = source_path if (source_path / "manifest.json").is_file() else source_path / chosen
    if not selected.is_dir() or selected.parent not in {source_path, source_path.parent}:
        raise StandbyError("Backup run находится вне разрешённого источника.")
    for item in selected.iterdir():
        target = destination / item.name
        if item.is_file():
            shutil.copy2(item, target)
    return destination, chosen


def _candidate_database_name(manifest: dict) -> str:
    compact = str(manifest["backup_run_id"]).replace("-", "")[:12]
    name = f"{settings.DENSTOCK_EMERGENCY_DB_PREFIX}{compact}"
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", name):
        raise StandbyError("Generated standby database name is unsafe.")
    return name


def _admin_connection(database_settings: dict):
    return psycopg.connect(
        host=database_settings.get("HOST") or "localhost",
        port=database_settings.get("PORT") or 5432,
        user=database_settings.get("USER") or "",
        password=database_settings.get("PASSWORD") or "",
        dbname="postgres",
        connect_timeout=10,
        autocommit=True,
    )


def create_database(name: str, *, database_settings=None) -> None:
    database_settings = database_settings or connection.settings_dict
    with _admin_connection(database_settings) as database:
        with database.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
            if cursor.fetchone():
                raise StandbyError("Candidate standby database уже существует.")
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database(name: str, *, database_settings=None) -> None:
    prefix = settings.DENSTOCK_EMERGENCY_DB_PREFIX
    if not name.startswith(prefix) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", name):
        raise StandbyError("Refusing to drop a database outside the emergency prefix.")
    database_settings = database_settings or connection.settings_dict
    if name == database_settings.get("NAME"):
        raise StandbyError("Refusing to drop the currently connected database.")
    with _admin_connection(database_settings) as database:
        with database.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                [name],
            )
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def _database_url(database_settings: dict, name: str) -> str:
    user = quote(str(database_settings.get("USER") or ""), safe="")
    password = quote(str(database_settings.get("PASSWORD") or ""), safe="")
    host = database_settings.get("HOST") or "localhost"
    port = database_settings.get("PORT") or 5432
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{quote(name, safe='')}"


def validate_candidate(name: str, media_root: Path, *, database_settings=None) -> dict:
    database_settings = database_settings or connection.settings_dict
    candidate_url = _database_url(database_settings, name)
    environment = {
        **os.environ,
        "DATABASE_URL": candidate_url,
        "DENSTOCK_TEST_DATABASE_URL": candidate_url,
        "DENSTOCK_MODE": "emergency-local",
        "DENSTOCK_INSTANCE_ID": settings.DENSTOCK_INSTANCE_ID,
        "DENSTOCK_EMERGENCY_DATABASE_NAME": name,
        "DENSTOCK_MEDIA_ROOT": str(media_root),
    }
    commands = [
        [sys.executable, "manage.py", "check"],
        [sys.executable, "manage.py", "migrate", "--check"],
        [sys.executable, "manage.py", "check_stock_balance"],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise StandbyError(
                f"Candidate validation failed for {command[-1]}: "
                f"{result.stderr or result.stdout or result.returncode}"
            )
    probe = subprocess.run(
        [sys.executable, "manage.py", "emergency_probe", "--json"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
    )
    if probe.returncode:
        raise StandbyError(f"Candidate probe failed: {probe.stderr or probe.returncode}")
    try:
        return json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise StandbyError("Candidate probe returned invalid JSON.") from exc


def _cleanup_removed(entries: list[dict], paths: EmergencyPaths) -> list[str]:
    errors = []
    for entry in entries:
        database_name = entry.get("database_name", "")
        folder_name = entry.get("slot", "")
        try:
            if database_name:
                drop_database(database_name)
            folder = (paths.standbys / folder_name).resolve()
            if folder.parent == paths.standbys.resolve() and folder.is_dir():
                shutil.rmtree(folder)
        except Exception as exc:  # cleanup must not invalidate the active standby
            errors.append(f"{database_name or folder_name}: {exc}")
    return errors


def refresh_standby(source: str, *, run_id=None, paths=None) -> dict:
    """Download, verify and restore a candidate before atomically activating it."""
    validate_database_target(mode="emergency-local")
    paths = paths or EmergencyPaths.configured()
    with control_lock(paths):
        lifecycle = load_control(paths).get("offline_lifecycle")
        if lifecycle:
            raise StandbyError(
                "Standby refresh запрещён: offline lifecycle уже начат "
                f"({lifecycle.get('status', 'unknown')})."
            )
        return _refresh_standby_locked(source, run_id=run_id, paths=paths)


def _refresh_standby_locked(source: str, *, run_id=None, paths: EmergencyPaths) -> dict:
    if OfflineSession.objects.filter(
        status__in=[
            OfflineSession.Status.ACTIVE,
            OfflineSession.Status.FREEZING,
            OfflineSession.Status.EXPORT_FAILED,
        ]
    ).exists():
        raise StandbyError("Standby refresh запрещён во время активной offline session.")
    staging = paths.staging / f"refresh-{uuid.uuid4().hex}"
    database_name = ""
    slot_dir = None
    activated = False
    try:
        fetched, source_run_id = fetch_backup(source, staging, run_id=run_id)
        report = validate_manifest(fetched, expected_source="production")
        if not report.ok:
            raise StandbyError("Backup verification failed: " + "; ".join(report.errors))
        manifest = report.manifest
        app_migrations = application_migration_state()
        if manifest["migration_fingerprint"] != app_migrations["fingerprint"]:
            raise StandbyError("Backup migration state несовместим с local application.")
        local_commit = settings.DENSTOCK_APP_COMMIT or backup._git_commit()
        if not local_commit or manifest["app_commit"] != local_commit:
            raise StandbyError("Backup application commit не совпадает с local application.")

        control = load_control(paths)
        old_active = control.get("active_standby")
        if old_active and old_active.get("backup_run_id") == manifest["backup_run_id"]:
            active_run = active_standby_run_dir(old_active, paths)
            active_report = validate_manifest(active_run, expected_source="production")
            if not active_report.ok or active_report.manifest != manifest:
                raise StandbyError("Backup run id повторно использован с другими данными.")
            shutil.rmtree(staging)
            record_event(
                "standby_sync",
                "unchanged",
                details={"backup_run_id": manifest["backup_run_id"]},
            )
            return old_active

        database_name = _candidate_database_name(manifest)
        slot = database_name.removeprefix(settings.DENSTOCK_EMERGENCY_DB_PREFIX)
        slot_dir = paths.standbys / slot
        if slot_dir.exists():
            raise StandbyError("Такой standby slot уже существует.")
        create_database(database_name)
        candidate_settings = {**connection.settings_dict, "NAME": database_name}
        backup.restore_db(
            fetched / manifest["database_dump_filename"], settings_dict=candidate_settings
        )

        media_root = staging / "media"
        if manifest.get("media_filename"):
            backup.restore_media(fetched / manifest["media_filename"], media_root=media_root)
        else:
            media_root.mkdir(parents=True, exist_ok=True)
        if media_tree_sha256(media_root) != manifest["media_tree_sha256"]:
            raise StandbyError("Restored media fingerprint differs from manifest.")
        probe = validate_candidate(database_name, media_root)
        if probe.get("data_state", {}).get("business_sha256") != manifest["data_state"].get(
            "business_sha256"
        ):
            raise StandbyError("Restored business data fingerprint differs from manifest.")
        if probe.get("migration_fingerprint") != manifest["migration_fingerprint"]:
            raise StandbyError("Restored migration fingerprint differs from manifest.")

        slot_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, slot_dir)
        control = load_control(paths)
        old_active = control.get("active_standby")
        previous = ([old_active] if old_active else []) + control.get("previous_standbys", [])
        keep_previous = max(settings.DENSTOCK_EMERGENCY_KEEP_STANDBY - 1, 1)
        removed = previous[keep_previous:]
        active = {
            "slot": slot,
            "database_name": database_name,
            "media_root": str(slot_dir / "media"),
            "manifest_path": str(slot_dir / "manifest.json"),
            "backup_run_id": manifest["backup_run_id"],
            "source_run_id": source_run_id,
            "backup_created_at": manifest["created_at"],
            "app_commit": manifest["app_commit"],
            "database_identity": manifest["database_identity"],
            "synced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        new_control = {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "active_standby": active,
            "previous_standbys": previous[:keep_previous],
            "offline_lifecycle": None,
        }
        save_control(new_control, paths)
        activated = True
        cleanup_errors = _cleanup_removed(removed, paths)
        record_event(
            "standby_sync",
            "success",
            details={
                "backup_run_id": manifest["backup_run_id"],
                "slot": slot,
                "cleanup_errors": cleanup_errors,
            },
        )
        return active
    except Exception as exc:
        if database_name and not activated:
            try:
                drop_database(database_name)
            except Exception:
                pass
        if staging.is_dir():
            shutil.rmtree(staging)
        if not activated and slot_dir and slot_dir.is_dir():
            shutil.rmtree(slot_dir)
        record_event("standby_sync", "failed", details={"error": str(exc)[:500]})
        if isinstance(exc, StandbyError):
            raise
        raise StandbyError(str(exc)) from exc
