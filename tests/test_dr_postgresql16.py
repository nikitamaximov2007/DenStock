"""Synthetic end-to-end disaster-recovery drill on an isolated PostgreSQL 16."""

import json
import subprocess
import tarfile
import time
import uuid

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from psycopg import sql

from apps.operations import backup
from apps.operations.dr_archive import encrypt_verified_run
from apps.operations.emergency_manifest import validate_manifest
from apps.operations.manifest_signing import verify_manifest
from tests.emergency_support import configure_test_trust


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_synthetic_pg16_encrypted_restore_drill(tmp_path, settings):
    if connection.vendor != "postgresql":
        pytest.skip("Isolated PostgreSQL 16 required")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        version = int(cursor.fetchone()[0])
    if version // 10000 != 16:
        pytest.skip("Exactly PostgreSQL 16 required")

    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-drill"
    settings.DENSTOCK_APP_COMMIT = "a" * 40
    private = tmp_path / "private"
    private.mkdir()
    (private / "customer-request.bin").write_bytes(b"synthetic-private")
    media = tmp_path / "media"
    media.mkdir()
    (media / "part.jpg").write_bytes(b"synthetic-public")
    user_model = get_user_model()
    user_model.objects.create(username="synthetic-dr-restore")
    source_settings = connection.settings_dict

    started = time.monotonic()
    run = backup.backup_all(
        root=tmp_path / "backups", settings_dict=source_settings,
        media_root=media, private_media_root=private,
    )
    key = tmp_path / "age-key.txt"
    generated = subprocess.run(
        ["age-keygen", "-o", str(key)], check=True, capture_output=True, text=True,
    )
    recipient = next(
        line.split(": ", 1)[1]
        for line in (generated.stdout + generated.stderr).splitlines()
        if line.startswith("Public key: ")
    )
    cipher = tmp_path / "bundle.tar.age"
    encrypt_verified_run(run, cipher, recipient)
    package = tmp_path / "bundle.tar"
    subprocess.run(
        ["age", "-d", "-i", str(key), "-o", str(package), str(cipher)],
        check=True, capture_output=True,
    )
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(package) as archive:
        archive.extractall(extracted, filter="data")
    verify_manifest(json.loads((extracted / "manifest.json").read_text()))
    assert validate_manifest(extracted).ok

    restored_name = "dr_restore_" + uuid.uuid4().hex[:12]
    common = {
        "host": source_settings.get("HOST") or "localhost",
        "port": source_settings.get("PORT") or 5432,
        "user": source_settings.get("USER") or "",
        "password": source_settings.get("PASSWORD") or "",
    }
    with psycopg.connect(dbname="postgres", autocommit=True, **common) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored_name)))
        try:
            subprocess.run(
                ["pg_restore", "--no-owner", "--no-acl", "--exit-on-error",
                 "-h", str(common["host"]), "-p", str(common["port"]),
                 "-U", common["user"], "-d", restored_name,
                 str(extracted / "db.dump")],
                check=True, capture_output=True,
            )
            with psycopg.connect(dbname=restored_name, **common) as restored:
                assert restored.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE username = %s").format(
                        sql.Identifier(user_model._meta.db_table)
                    ),
                    ("synthetic-dr-restore",),
                ).fetchone()[0] == 1
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                sql.Identifier(restored_name)
            ))
    restored_media = tmp_path / "restored_media"
    restored_private = tmp_path / "restored_private"
    backup.restore_media(extracted / "media.tar.gz", media_root=restored_media)
    backup.restore_media(extracted / "private_media.tar.gz", media_root=restored_private)
    assert (restored_media / "part.jpg").read_bytes() == b"synthetic-public"
    assert (restored_private / "customer-request.bin").read_bytes() == b"synthetic-private"
    elapsed = time.monotonic() - started
    print(f"SYNTHETIC_DR_RTO_SECONDS={elapsed:.3f}")
    assert elapsed < 120
