"""Independent security regressions for production backup trust boundaries."""

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.files.storage import FileSystemStorage
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.customer_requests.attachments import cleanup_attachment
from apps.customer_requests.models import CustomerRequest, TelegramConversation, TelegramMessage
from apps.operations import backup, restore
from apps.operations.emergency_state import sha256_file
from apps.operations.manifest_signing import verify_emergency_manifest
from apps.operations.models import DeploymentState
from apps.operations.private_media import create_archive
from apps.operations.standby import refresh_standby
from apps.operations.write_guard import acquire_failover_lock, lifecycle_write
from tests.emergency_support import sign_production_manifest
from tests.test_emergency_standby import _source_backup

pytest_plugins = ("tests.test_emergency_standby", "tests.test_private_media_backup")


def test_emergency_final_backup_uses_an_independent_signing_key(production, settings):
    private = Ed25519PrivateKey.generate()
    path = production["tmp"] / "emergency-private-key.pem"
    public = production["tmp"] / "emergency-public-key.pem"
    path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    public.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    settings.DENSTOCK_EMERGENCY_MANIFEST_SIGNING_KEY_PATH = str(path)
    settings.DENSTOCK_EMERGENCY_MANIFEST_PUBLIC_KEY_PATH = str(public)
    settings.DENSTOCK_EMERGENCY_MANIFEST_SIGNING_KEY_ID = "synthetic-emergency-1"
    settings.DENSTOCK_MODE = "emergency-local"

    run = production["make"](trigger="emergency_final")
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))

    verify_emergency_manifest(manifest)
    assert manifest["signature"]["key_id"] == "synthetic-emergency-1"


@pytest.mark.django_db
@pytest.mark.parametrize("signature", [None, {}])
def test_production_rejects_unsigned_backup_after_signature_removal(
    tmp_path, settings, monkeypatch, signature
):
    """A signed backup must not become restorable by deleting its signature."""
    settings.DENSTOCK_MODE = "production"
    settings.BACKUP_ROOT = tmp_path
    run = tmp_path / "synthetic"
    run.mkdir()
    (run / "db.sqlite3").write_bytes(b"synthetic database payload")
    manifest = {
        "created_at": "2026-10-11T00:00:00+00:00",
        "engine": "sqlite",
        "db_file": "db.sqlite3",
        "source_environment": "production",
        "schema_version": 2,
    }
    if signature is not None:
        manifest["signature"] = signature
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(restore.backup, "_project_version", lambda: None)

    report = restore.verify_backup("synthetic")

    assert not report.ok
    assert any("подпис" in error.lower() for error in report.errors)


@pytest.mark.skipif(connection.vendor != "sqlite", reason="Synthetic fixture uses SQLite")
def test_forged_private_payload_and_rewritten_checksums_cannot_downgrade_signature(
    production, tmp_path
):
    run = production["make"]()
    forged_source = tmp_path / "forged-private"
    forged_source.mkdir()
    (forged_source / "synthetic.bin").write_bytes(b"forged private content")
    forged_archive, inventory = create_archive(forged_source, tmp_path)
    destination = run / "private_media.tar.gz"
    destination.write_bytes(forged_archive.read_bytes())
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("signature")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    manifest.update(
        private_media_sha256=digest,
        private_media_tree_sha256=inventory.tree_sha256,
        private_media_file_count=inventory.files,
        private_media_bytes=inventory.bytes,
    )
    manifest["sha256"]["private_media.tar.gz"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = restore.verify_backup(run.name)

    assert not report.ok
    assert any("подпис" in error.lower() for error in report.errors)


@pytest.mark.django_db
def test_emergency_standby_installs_private_files_from_verified_backup(
    tmp_path, standby_runtime, settings
):
    paths, _, _ = standby_runtime
    source = _source_backup(tmp_path / "source", settings=settings)
    private = tmp_path / "source-private"
    private.mkdir()
    (private / "synthetic.bin").write_bytes(b"synthetic private attachment")
    archive, inventory = create_archive(private, source)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("signature")
    manifest.update(
        private_media_status="included",
        private_media_filename=archive.name,
        private_media_sha256=sha256_file(archive),
        private_media_tree_sha256=inventory.tree_sha256,
        private_media_file_count=inventory.files,
        private_media_bytes=inventory.bytes,
        sha256={archive.name: sha256_file(archive)},
    )
    sign_production_manifest(manifest, settings)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    active = refresh_standby(str(source.parent), paths=paths)

    assert (paths.standbys / active["slot"] / "private_media" / "synthetic.bin").read_bytes() == (
        b"synthetic private attachment"
    )


def test_emergency_web_reads_private_files_from_the_selected_standby_slot():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.emergency.yml").read_text()
    assert (
        "PRIVATE_MEDIA_ROOT: /app/.emergency/standbys/${EMERGENCY_SLOT:-inactive}/private_media"
        in compose
    )


def test_rotation_keeps_last_signed_backup_when_newer_run_has_only_forged_marker(
    production
):
    old = production["make"]()
    newer = old.parent / "2099-01-01_00-00-00"
    newer.mkdir()
    (newer / "manifest.json").write_text(json.dumps({"verification_status": "verified"}))

    backup.prune_old_runs(old.parent, keep_last=1)

    assert old.exists(), "the only signed complete backup must remain"


def test_two_backup_publishers_cannot_reuse_the_same_generation_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "timestamp", lambda: "2026-10-11_03-00-12")
    first = backup.new_run_dir(tmp_path)
    (first / "db.dump").write_bytes(b"first publisher")

    with pytest.raises(backup.OperationsError, match="существует"):
        backup.new_run_dir(tmp_path)

    assert (first / "db.dump").read_bytes() == b"first publisher"


def test_production_backup_fails_when_public_media_volume_is_missing(production):
    missing = production["tmp"] / "missing-public-volume"
    with pytest.raises(backup.OperationsError):
        production["make"](media_root=missing)


def test_public_media_change_during_backup_cannot_create_verified_mixed_snapshot(
    production, monkeypatch
):
    original = backup.backup_media

    def changed_media(*args, **kwargs):
        (production["media"] / "part.jpg").write_bytes(b"new photo after database snapshot")
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, "backup_media", changed_media)

    with pytest.raises(backup.OperationsError, match="media"):
        production["make"]()


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_backup_lock_prevents_deletion_of_db_referenced_attachment(tmp_path, settings):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL 16 required")
    DeploymentState.get_solo()
    request = CustomerRequest.objects.create(
        customer_name="Synthetic", customer_phone="0", preferred_messenger="telegram",
        privacy_policy_version="v", personal_data_consent_version="v",
        consent_purpose="synthetic", consent_accepted_at=timezone.now(),
        submission_key_hash="a" * 64,
    )
    conversation = TelegramConversation.objects.create(request=request)
    name = "customer_requests/synthetic.txt"
    row = TelegramMessage.objects.create(
        conversation=conversation, direction=TelegramMessage.Direction.OPERATOR,
        attachment=name,
    )
    private = tmp_path / "private"
    (private / "customer_requests").mkdir(parents=True)
    path = private / name
    path.write_bytes(b"synthetic attachment")
    row.attachment.storage = FileSystemStorage(location=private)
    settings.DENSTOCK_MODE = "production"
    started = threading.Event()

    def worker():
        close_old_connections()
        try:
            started.set()
            cleanup_attachment(row)
        finally:
            close_old_connections()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic(), lifecycle_write():
                acquire_failover_lock(exclusive=True)
                future = executor.submit(worker)
                assert started.wait(10)
                time.sleep(0.25)
                assert not future.done(), "the database update must wait for the backup lock"
                db_value = TelegramMessage.objects.get(pk=row.pk).attachment.name
                assert not (db_value and not path.exists())
            future.result(timeout=10)
        assert TelegramMessage.objects.get(pk=row.pk).attachment.name == ""
        assert not path.exists()
    finally:
        settings.DENSTOCK_MODE = "test"


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_postgresql_backup_verification_reads_data_blocks_not_only_toc(tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL 16 required")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        if int(cursor.fetchone()[0]) // 10000 != 16:
            pytest.skip("PostgreSQL 16 required")
    table = "synthetic_backup_probe_" + uuid.uuid4().hex[:12]
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TABLE {table} (id integer PRIMARY KEY, payload text)")
        cursor.execute(
            f"INSERT INTO {table} SELECT n, repeat(md5(n::text), 500) "
            "FROM generate_series(1, 100) AS n"
        )
    db = connection.settings_dict
    archive = tmp_path / "synthetic.dump"
    environment = os.environ.copy()
    if db.get("PASSWORD"):
        environment["PGPASSWORD"] = str(db["PASSWORD"])
    try:
        subprocess.run(
            [backup.pg_binary("pg_dump", 16), "-Fc", "-t", table, "-f", str(archive),
             "-h", str(db.get("HOST") or "localhost"),
             "-p", str(db.get("PORT") or "5432"),
             "-U", str(db.get("USER") or ""), str(db["NAME"])],
            env=environment, check=True, capture_output=True,
        )
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE {table}")
    damaged = bytearray(archive.read_bytes())
    damaged[int(len(damaged) * 0.9)] ^= 1
    archive.write_bytes(damaged)
    assert subprocess.run(
        [backup.pg_binary("pg_restore", 16), "--list", str(archive)],
        capture_output=True,
    ).returncode == 0

    with pytest.raises(backup.OperationsError):
        backup.verify_database_payload(archive, "postgresql")
