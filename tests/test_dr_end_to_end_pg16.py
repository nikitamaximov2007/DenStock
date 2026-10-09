"""Synthetic end-to-end DR on real PostgreSQL 16 + age with a filesystem rclone stand-in.

dump + media + private_media -> signed manifest -> age -> two "clouds" -> rotation
-> Mac pull -> signature/checksum checks -> decrypt -> restore into a NEW database
and NEW directories -> induced failure leaves nothing behind.  No real cloud.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from psycopg import sql

from apps.operations import backup, dr_status
from apps.operations.dr_archive import encrypt_verified_run, signed_cipher_receipt
from apps.operations.dr_restore import RestoreError, restore_bundle
from apps.operations.emergency_manifest import read_manifest
from scripts.operations import dr_mac_pull, dr_upload
from tests.emergency_support import configure_test_trust

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]

DESTINATIONS = ["yandex=s3=yx:bucket/denisstock", "google=drive=gd:DenisStock"]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _stage(run, staging, recipient):
    target = staging / run.name
    target.mkdir(parents=True)
    encrypt_verified_run(run, target / "bundle.tar.age", recipient)
    receipt = signed_cipher_receipt(
        run.name, target / "bundle.tar.age",
        backup_created_at=read_manifest(run / "manifest.json")["created_at"],
    )
    (target / "receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")


def test_synthetic_disaster_recovery_end_to_end(tmp_path, settings, monkeypatch, capsys):
    if connection.vendor != "postgresql":
        pytest.skip("Isolated PostgreSQL 16 required")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        if int(cursor.fetchone()[0]) // 10000 != 16:
            pytest.skip("Exactly PostgreSQL 16 required")
    started = time.monotonic()
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID = "production-1"
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-e2e"
    settings.DENSTOCK_APP_COMMIT = "b" * 40
    public = Path(settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "rclone"
    shutil.copyfile(Path(__file__).with_name("dr_rclone_stub.py"), stub)
    stub.write_text(stub.read_text().replace("/usr/bin/env python3", sys.executable, 1))
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("DR_STUB_ROOT", str(tmp_path / "clouds"))

    media, private = tmp_path / "media", tmp_path / "private"
    (media / "parts").mkdir(parents=True)
    (private / "requests").mkdir(parents=True)
    (media / "parts" / "photo.jpg").write_bytes(b"synthetic-public-photo")
    (private / "requests" / "scan.pdf").write_bytes(b"synthetic-private-scan")
    user_model = get_user_model()
    user_model.objects.create(username="synthetic-e2e-first")

    identity = tmp_path / "age.txt"
    out = subprocess.run(["age-keygen", "-o", str(identity)], check=True,
                         capture_output=True, text=True)
    recipient = re.search(r"age1[a-z0-9]+", out.stdout + out.stderr).group(0)
    staging, status = tmp_path / "staging", tmp_path / "dr-status.json"

    # Generation 1 to both clouds.
    first = backup.backup_all(root=tmp_path / "backups", settings_dict=connection.settings_dict,
                              media_root=media, private_media_root=private)
    _stage(first, staging, recipient)
    assert dr_upload.run(staging, DESTINATIONS, public, status,
                         allow_best_effort_budget=True) == 0

    # Generation 2 (newer data) replaces generation 1 only after it is verified.
    time.sleep(1.1)
    user_model.objects.create(username="synthetic-e2e-second")
    second = backup.backup_all(root=tmp_path / "backups", settings_dict=connection.settings_dict,
                               media_root=media, private_media_root=private)
    _stage(second, staging, recipient)
    assert dr_upload.run(staging, DESTINATIONS, public, status,
                         allow_best_effort_budget=True) == 0
    for cloud in (tmp_path / "clouds" / "yx" / "bucket" / "denisstock",
                  tmp_path / "clouds" / "gd" / "DenisStock"):
        assert sorted(p.name for p in cloud.iterdir()) == [second.name]
    assert dr_status.problems(status, ["yandex", "google"], timedelta(hours=36)) == []

    # MacBook pulls from both clouds through the real subprocess code path.
    mac = tmp_path / "mac"
    monkeypatch.setattr("sys.argv", [
        "dr_mac_pull", "--remote", "yx:bucket/denisstock", "--remote", "gd:DenisStock",
        "--root", str(mac), "--public-key", str(public),
    ])
    assert dr_mac_pull.main() == 0
    bundle = mac / second.name / "bundle.tar.age"
    assert bundle.is_file()
    assert b"synthetic-private-scan" not in bundle.read_bytes()

    # Restore from the Mac copy into a new database and new directories.
    s = connection.settings_dict
    pg = {"host": s.get("HOST") or "localhost", "port": s.get("PORT") or 5432,
          "user": s.get("USER") or "", "password": s.get("PASSWORD") or ""}
    work = tmp_path / "work"
    work.mkdir()
    target_db = "dr_e2e_" + uuid.uuid4().hex[:10]
    restored = tmp_path / "restored"
    report = restore_bundle(
        bundle, identity, public, pg=pg, new_database=target_db,
        media_target=restored / "media", private_target=restored / "private",
        work_parent=work,
    )
    try:
        with psycopg.connect(dbname=target_db, **pg) as db:
            names = {row[0] for row in db.execute(
                sql.SQL("SELECT username FROM {}").format(
                    sql.Identifier(user_model._meta.db_table)))}
        assert {"synthetic-e2e-first", "synthetic-e2e-second"} <= names
        assert _sha(restored / "media" / "parts" / "photo.jpg") == \
            _sha(media / "parts" / "photo.jpg")
        assert _sha(restored / "private" / "requests" / "scan.pdf") == \
            _sha(private / "requests" / "scan.pdf")
        assert list(work.iterdir()) == []
    finally:
        with psycopg.connect(dbname="postgres", autocommit=True, **pg) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(target_db)))

    # Induced failure (pg_restore fails) leaves no database, directories or plaintext.
    failed_db = "dr_e2e_" + uuid.uuid4().hex[:10]
    failed = tmp_path / "failed"
    with pytest.raises(RestoreError):
        restore_bundle(
            bundle, identity, public, pg=pg, new_database=failed_db,
            media_target=failed / "media", private_target=failed / "private",
            work_parent=work, pg_restore="false",
        )
    with psycopg.connect(dbname="postgres", autocommit=True, **pg) as admin:
        assert admin.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                             (failed_db,)).fetchone() is None
    assert not (failed / "media").exists() and not (failed / "private").exists()
    assert list(failed.glob(".restore-*")) == [] if failed.exists() else True
    assert list(work.iterdir()) == []
    print(f"SYNTHETIC_E2E_SECONDS={time.monotonic() - started:.2f} "
          f"(restore {report.seconds:.2f}s; NOT a production RTO)")
