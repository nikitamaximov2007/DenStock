import json

import pytest
from django.test import override_settings

from apps.operations import backup
from apps.operations.emergency_manifest import SCHEMA_VERSION, validate_manifest
from apps.operations.emergency_state import migration_state, record_event, sha256_file
from apps.operations.models import DeploymentState


def _manifest(run, db_path, media_path=None, **overrides):
    values = {
        "schema_version": SCHEMA_VERSION,
        "backup_run_id": "a63f4a56-a616-4a6d-ad1d-a7bace93130f",
        "created_at": "2026-08-12T10:00:00+05:00",
        "source_environment": "production",
        "source_instance_id": "production",
        "app_commit": "a" * 40,
        "database_name": "denstock",
        "database_identity": "52347a14-d939-45e6-a397-06c79ef257f2",
        "database_dump_filename": db_path.name,
        "database_sha256": sha256_file(db_path),
        "media_filename": media_path.name if media_path else None,
        "media_sha256": sha256_file(media_path) if media_path else None,
        "media_tree_sha256": "b" * 64,
        "migration_fingerprint": "c" * 64,
        "migration_state": [],
        "data_state": {"business_sha256": "d" * 64, "tables": {}},
        "storage_origin": "yandex-object-storage",
        "verification_status": "verified",
    }
    values.update(overrides)
    (run / "manifest.json").write_text(json.dumps(values), encoding="utf-8")
    return values


def test_valid_emergency_manifest(tmp_path):
    db_path = tmp_path / "db.dump"
    db_path.write_bytes(b"database")
    media_path = tmp_path / "media.tar.gz"
    media_path.write_bytes(b"media")
    _manifest(tmp_path, db_path, media_path)

    report = validate_manifest(tmp_path, expected_source="production")

    assert report.ok
    assert report.errors == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"database_sha256": "0" * 64}, "контрольная сумма базы"),
        ({"schema_version": 999}, "неподдерживаемая версия"),
        ({"source_environment": "development"}, "ожидался source_environment"),
        ({"verification_status": "failed"}, "не помечен как verified"),
    ],
)
def test_manifest_rejects_invalid_metadata(tmp_path, override, message):
    db_path = tmp_path / "db.dump"
    db_path.write_bytes(b"database")
    _manifest(tmp_path, db_path, **override)

    report = validate_manifest(tmp_path, expected_source="production")

    assert not report.ok
    assert any(message in error for error in report.errors)


def test_manifest_rejects_bad_media_hash_and_missing_database(tmp_path):
    db_path = tmp_path / "db.dump"
    db_path.write_bytes(b"database")
    media_path = tmp_path / "media.tar.gz"
    media_path.write_bytes(b"media")
    _manifest(tmp_path, db_path, media_path, media_sha256="0" * 64)
    db_path.unlink()

    report = validate_manifest(tmp_path)

    assert any("файл базы" in error for error in report.errors)
    assert any("контрольная сумма media" in error for error in report.errors)


@pytest.mark.django_db
@override_settings(
    DENSTOCK_MODE="production",
    DENSTOCK_INSTANCE_ID="production-test",
    DENSTOCK_APP_COMMIT="a" * 40,
)
def test_backup_all_writes_verified_v2_manifest(tmp_path, db, settings, monkeypatch):
    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"sqlite-copy")
    media = tmp_path / "media"
    media.mkdir()
    (media / "photo.jpg").write_bytes(b"photo")
    settings_dict = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(source)}
    monkeypatch.setattr(backup, "verify_database_payload", lambda *args: None)
    monkeypatch.setattr(backup, "business_state_marker", lambda: {
        "database_identity": str(DeploymentState.get_solo().database_identity),
        "business_generation": 0,
        "business_sha256": "d" * 64,
        "tables": {},
    })

    run = backup.backup_all(
        root=tmp_path / "backups", settings_dict=settings_dict, media_root=media
    )
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["source_environment"] == "production"
    assert manifest["verification_status"] == "verified"
    assert manifest["database_sha256"] == sha256_file(run / "db.sqlite3")
    assert manifest["media_sha256"] == sha256_file(run / "media.tar.gz")
    assert manifest["migration_fingerprint"] == migration_state()["fingerprint"]


@pytest.mark.django_db
def test_emergency_audit_event_has_no_secret_values():
    event = record_event(
        "standby_sync",
        "failed",
        details={"password": "do-not-store", "run_id": "safe"},
    )

    assert event.details == {"password": "[REDACTED]", "run_id": "safe"}
