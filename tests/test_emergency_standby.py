import json
from pathlib import Path

import pytest
from django.db import connection
from django.test import override_settings

from apps.operations.emergency_lifecycle import _active_standby
from apps.operations.emergency_manifest import SCHEMA_VERSION
from apps.operations.emergency_state import (
    application_migration_state,
    media_tree_sha256,
    sha256_file,
)
from apps.operations.models import OfflineSession
from apps.operations.standby import (
    EmergencyPaths,
    StandbyError,
    active_standby_run_dir,
    load_control,
    refresh_standby,
)

COMMIT = "a" * 40


def _source_backup(root: Path, *, run_id="2026-08-12_10-00-00", app_commit=COMMIT) -> Path:
    run = root / run_id
    run.mkdir(parents=True)
    database = run / "db.dump"
    database.write_bytes(b"synthetic-postgres-dump")
    migrations = application_migration_state()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "backup_run_id": "d7919779-6c24-43cb-bb78-181f61a335d5",
        "created_at": "2026-08-12T10:00:00+05:00",
        "verified_at": "2026-08-12T10:01:00+05:00",
        "source_environment": "production",
        "source_instance_id": "production",
        "app_commit": app_commit,
        "database_name": "denstock",
        "database_identity": "52347a14-d939-45e6-a397-06c79ef257f2",
        "database_dump_filename": database.name,
        "database_sha256": sha256_file(database),
        "media_filename": None,
        "media_sha256": None,
        "media_tree_sha256": media_tree_sha256(run / "empty-media"),
        "migration_fingerprint": migrations["fingerprint"],
        "migration_state": migrations["available"],
        "data_state": {
            "database_identity": "52347a14-d939-45e6-a397-06c79ef257f2",
            "business_generation": 0,
            "business_sha256": "d" * 64,
            "tables": {},
        },
        "storage_origin": "test",
        "verification_status": "verified",
        "consistency": "database_snapshot",
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


@pytest.fixture
def standby_runtime(tmp_path, monkeypatch, settings):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_INSTANCE_ID = "warehouse-pc-test"
    settings.DENSTOCK_APP_COMMIT = COMMIT
    settings.DENSTOCK_EMERGENCY_DB_PREFIX = "denstock_emergency_"
    settings.DENSTOCK_EMERGENCY_KEEP_STANDBY = 2
    paths = EmergencyPaths(tmp_path / "emergency")
    created = []
    dropped = []
    monkeypatch.setattr("apps.operations.standby.validate_database_target", lambda **kwargs: None)
    monkeypatch.setattr("apps.operations.standby.create_database", created.append)
    monkeypatch.setattr("apps.operations.standby.drop_database", dropped.append)
    monkeypatch.setattr("apps.operations.standby.backup.restore_db", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "apps.operations.standby.backup.restore_media", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("apps.operations.standby.backup._git_commit", lambda: COMMIT)
    monkeypatch.setattr(
        "apps.operations.standby.validate_candidate",
        lambda *args, **kwargs: {
            "migration_fingerprint": application_migration_state()["fingerprint"],
            "data_state": {"business_sha256": "d" * 64},
        },
    )
    return paths, created, dropped


@pytest.mark.django_db
def test_successful_standby_refresh_is_activated_atomically(tmp_path, standby_runtime):
    paths, created, dropped = standby_runtime
    source = tmp_path / "source"
    _source_backup(source)

    active = refresh_standby(str(source), paths=paths)

    assert active["database_name"] == "denstock_emergency_d79197796c24"
    assert created == [active["database_name"]]
    assert dropped == []
    assert load_control(paths)["active_standby"] == active
    assert Path(active["manifest_path"]).is_file()


@pytest.mark.django_db
def test_active_standby_validates_manifest_file_recorded_in_control(
    tmp_path, standby_runtime, monkeypatch
):
    paths, _, _ = standby_runtime
    source = tmp_path / "source"
    _source_backup(source)
    active = refresh_standby(str(source), paths=paths)
    monkeypatch.setitem(connection.settings_dict, "NAME", active["database_name"])

    selected, manifest = _active_standby(paths)

    assert selected == active
    assert manifest["backup_run_id"] == active["backup_run_id"]


@pytest.mark.django_db
def test_repeated_refresh_of_same_verified_backup_is_idempotent(tmp_path, standby_runtime):
    paths, created, dropped = standby_runtime
    source = tmp_path / "source"
    _source_backup(source)

    first = refresh_standby(str(source), paths=paths)
    second = refresh_standby(str(source), paths=paths)

    assert second == first
    assert created == [first["database_name"]]
    assert dropped == []


@pytest.mark.django_db
def test_active_standby_rejects_control_path_outside_recorded_slot(
    tmp_path, standby_runtime
):
    paths, _, _ = standby_runtime
    source = tmp_path / "source"
    _source_backup(source)
    active = refresh_standby(str(source), paths=paths)
    active["manifest_path"] = str(tmp_path / "outside" / "manifest.json")

    with pytest.raises(StandbyError, match="paths"):
        active_standby_run_dir(active, paths)


@pytest.mark.django_db
def test_broken_download_keeps_previous_standby(tmp_path, standby_runtime):
    paths, created, dropped = standby_runtime
    paths.root.mkdir(parents=True)
    previous = {
        "schema_version": 1,
        "active_standby": {"database_name": "denstock_emergency_previous"},
        "previous_standbys": [],
        "offline_lifecycle": None,
    }
    paths.control.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(StandbyError, match="источник backup не найден"):
        refresh_standby(str(tmp_path / "missing"), paths=paths)

    assert load_control(paths) == previous
    assert created == []
    assert dropped == []


@pytest.mark.django_db
def test_newer_backup_with_incompatible_app_commit_keeps_previous_ready_standby(
    tmp_path, standby_runtime
):
    paths, created, dropped = standby_runtime
    source = tmp_path / "source"
    _source_backup(source, app_commit="b" * 40)
    paths.root.mkdir(parents=True)
    previous = {
        "schema_version": 1,
        "active_standby": {"database_name": "denstock_emergency_previous"},
        "previous_standbys": [],
        "offline_lifecycle": None,
    }
    paths.control.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(StandbyError, match="application commit"):
        refresh_standby(str(source), paths=paths)

    assert load_control(paths) == previous
    assert created == []
    assert dropped == []


@pytest.mark.django_db
def test_restore_or_health_failure_rolls_back_candidate(tmp_path, standby_runtime, monkeypatch):
    paths, created, dropped = standby_runtime
    source = tmp_path / "source"
    _source_backup(source)
    paths.root.mkdir(parents=True)
    previous = {
        "schema_version": 1,
        "active_standby": {"database_name": "denstock_emergency_previous"},
        "previous_standbys": [],
        "offline_lifecycle": None,
    }
    paths.control.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(
        "apps.operations.standby.validate_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(StandbyError("health failure")),
    )

    with pytest.raises(StandbyError, match="health failure"):
        refresh_standby(str(source), paths=paths)

    assert load_control(paths) == previous
    assert dropped == created


@pytest.mark.django_db
def test_refresh_is_blocked_while_offline_session_is_active(tmp_path, standby_runtime):
    paths, created, dropped = standby_runtime
    OfflineSession.objects.create(
        kind=OfflineSession.Kind.UNPLANNED,
        status=OfflineSession.Status.ACTIVE,
        local_hostname="warehouse-pc",
        instance_id="warehouse-pc-test",
        base_backup_run_id="run",
        base_backup_created_at="2026-08-12T10:00:00+05:00",
        base_manifest={},
        base_data_marker={},
        base_migration_fingerprint="a" * 64,
    )

    with pytest.raises(StandbyError, match="запрещён"):
        refresh_standby(str(tmp_path), paths=paths)

    assert created == []
    assert dropped == []


@pytest.mark.django_db
def test_control_lifecycle_blocks_scheduled_refresh(tmp_path, standby_runtime):
    paths, created, dropped = standby_runtime
    paths.root.mkdir(parents=True)
    paths.control.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_standby": {"database_name": "denstock_emergency_previous"},
                "previous_standbys": [],
                "offline_lifecycle": {"status": "active", "session_id": "session"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StandbyError, match="offline lifecycle"):
        refresh_standby(str(tmp_path), paths=paths)

    assert created == []
    assert dropped == []


@pytest.mark.parametrize(
    ("mode", "engine", "host", "name", "message"),
    [
        (
            "emergency-local",
            "django.db.backends.postgresql",
            "185.250.44.206",
            "denstock_emergency_x",
            "production",
        ),
        ("emergency-local", "django.db.backends.postgresql", "localhost", "denstock", "prefix"),
        ("emergency-local", "django.db.backends.sqlite3", "", "denstock_emergency_x", "PostgreSQL"),
        ("production", "django.db.backends.postgresql", "db", "denstock_emergency_x", "emergency"),
        ("production", "django.db.backends.sqlite3", "db", "denstock", "PostgreSQL"),
        ("production", "django.db.backends.postgresql", "localhost", "denstock", "emergency"),
        ("production", "django.db.backends.postgresql", "unknown", "denstock", "allowlisted"),
    ],
)
@override_settings(
    DENSTOCK_EMERGENCY_DB_PREFIX="denstock_emergency_",
    DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS=["localhost", "127.0.0.1", "emergency-db"],
    DENSTOCK_PRODUCTION_DB_HOSTS=["185.250.44.206"],
)
def test_environment_database_guard(mode, engine, host, name, message):
    from apps.operations.emergency_environment import EmergencySafetyError, validate_database_target

    with pytest.raises(EmergencySafetyError, match=message):
        validate_database_target(
            mode=mode,
            database={"ENGINE": engine, "HOST": host, "NAME": name},
        )


@override_settings(
    DENSTOCK_EMERGENCY_DB_PREFIX="denstock_emergency_",
    DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS=["localhost", "emergency-db"],
    DENSTOCK_PRODUCTION_DB_HOSTS=["db", "185.250.44.206"],
)
def test_production_database_guard_accepts_allowlisted_postgresql():
    from apps.operations.emergency_environment import validate_database_target

    validate_database_target(
        mode="production",
        database={
            "ENGINE": "django.db.backends.postgresql",
            "HOST": "db",
            "NAME": "denstock",
        },
    )
