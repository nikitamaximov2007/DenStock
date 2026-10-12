"""Backup-only RC on a real PostgreSQL 16, synthetic data only.

DATA -> backup_all -> Ed25519 sign -> verify_backup + validate_manifest ->
simulated offsite upload (local-only fake rclone) -> read back -> restore into a
NEW database and NEW directories -> compare.  Plus the clean-cluster role
prerequisite found on 2026-10-11 (dump ACLs name ``denstock_public``).
"""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from psycopg import sql

from apps.operations import backup, restore
from apps.operations.emergency_manifest import validate_manifest
from apps.operations.manifest_signing import verify_manifest
from apps.operations.private_media import restore_archive
from tests.emergency_support import configure_test_trust

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]

FAKE_RCLONE = Path(__file__).resolve().parent / "fake_rclone.py"


def _require_pg16():
    if connection.vendor != "postgresql":
        pytest.skip("Isolated PostgreSQL 16 required")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        if int(cursor.fetchone()[0]) // 10000 != 16:
            pytest.skip("Exactly PostgreSQL 16 required")


def _pg():
    s = connection.settings_dict
    return {"host": s.get("HOST") or "localhost", "port": s.get("PORT") or 5432,
            "user": s.get("USER") or "", "password": s.get("PASSWORD") or ""}


def _admin(statement, *args):
    with psycopg.connect(dbname="postgres", autocommit=True, **_pg()) as admin:
        admin.execute(statement, *args)


def _tree(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _row_counts(dbname):
    with psycopg.connect(dbname=dbname, **_pg()) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )]
        return {
            table: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            for table in tables
        }


def _rclone(tmp_path, *args):
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "rclone"
    if not shim.exists():
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_RCLONE}" "$@"\n')
        shim.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(["rclone", *args], env=env, capture_output=True, text=True)


@pytest.fixture
def production_pg(tmp_path, settings):
    _require_pg16()
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-pg16-rc"
    settings.DENSTOCK_APP_COMMIT = "e" * 40
    settings.BACKUP_ROOT = tmp_path / "backups"
    media, private = tmp_path / "media", tmp_path / "private"
    (media / "parts").mkdir(parents=True)
    (media / "customer_requests").mkdir()
    (private / "customer_requests" / "41").mkdir(parents=True)
    (private / "catalog-imports").mkdir()
    (private / "ai-support").mkdir()
    (media / "parts" / "photo.jpg").write_bytes(b"synthetic public photo")
    (media / "customer_requests" / "legacy.pdf").write_bytes(b"synthetic legacy attachment")
    (private / "customer_requests" / "41" / "scan.pdf").write_bytes(b"synthetic private scan")
    (private / "catalog-imports" / "import.bin").write_bytes(os.urandom(256 * 1024))
    (private / "ai-support" / "screen.png").write_bytes(b"\x89PNG synthetic")
    settings.PRIVATE_MEDIA_ROOT = private
    return {"tmp": tmp_path, "media": media, "private": private, "settings": settings}


def test_full_path_backup_sign_upload_read_back_restore_into_new_targets(production_pg):
    tmp, settings = production_pg["tmp"], production_pg["settings"]
    get_user_model().objects.create(username="synthetic-rc-user")
    source_db = connection.settings_dict["NAME"]

    run = backup.backup_all(
        settings_dict=connection.settings_dict, media_root=production_pg["media"],
        private_media_root=production_pg["private"], trigger="automatic", keep_last=14,
    )
    manifest = json.loads((run / "manifest.json").read_text())
    verify_manifest(manifest)
    assert manifest["private_media_status"] == "included"
    assert manifest["private_media_file_count"] == 3
    assert validate_manifest(run, expected_source="production").ok
    assert restore.verify_backup(run.name).ok

    remote = tmp / "remote-bucket" / run.name
    assert _rclone(tmp, "copy", str(run), str(remote)).returncode == 0
    check = _rclone(tmp, "check", str(run), str(remote))
    assert check.returncode == 0 and "0 differences" in check.stdout
    readback_root = tmp / "readback"
    assert _rclone(tmp, "copy", str(remote), str(readback_root / run.name)).returncode == 0
    settings.BACKUP_ROOT = readback_root
    report = restore.verify_backup(run.name)
    assert report.ok, report.errors
    assert report.private_media_file == "private_media.tar.gz"
    readback = readback_root / run.name

    target_db = "rc_" + uuid.uuid4().hex[:10]
    _admin(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
    try:
        warnings = backup.restore_db(
            readback / manifest["database_dump_filename"],
            settings_dict={**connection.settings_dict, "NAME": target_db},
        )
        assert warnings == []
        assert _row_counts(target_db) == _row_counts(source_db)
        with psycopg.connect(dbname=target_db, **_pg()) as restored:
            assert restored.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE username=%s").format(
                    sql.Identifier(get_user_model()._meta.db_table)),
                ("synthetic-rc-user",),
            ).fetchone()[0] == 1
    finally:
        _admin(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(target_db)))

    new_media, new_private = tmp / "new-media", tmp / "new-private"
    backup.restore_media(readback / "media.tar.gz", media_root=new_media)
    restore_archive(readback / "private_media.tar.gz", new_private,
                    expected=restore._manifest_private_inventory(manifest))
    assert _tree(new_media) == _tree(production_pg["media"])
    assert _tree(new_private) == _tree(production_pg["private"])


def test_clean_cluster_needs_the_public_catalog_role_before_restore(production_pg):
    """pg_restore replays GRANTs; a clean cluster without the role fails loudly."""
    role = "denstock_public_rc_" + uuid.uuid4().hex[:8]
    probe = "rc_role_probe_" + uuid.uuid4().hex[:8]
    _admin(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE TABLE {} (id integer)").format(
            sql.Identifier(probe)).as_string(cursor.connection))
        cursor.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(
            sql.Identifier(probe), sql.Identifier(role)).as_string(cursor.connection))
    try:
        run = backup.backup_all(
            settings_dict=connection.settings_dict, media_root=production_pg["media"],
            private_media_root=production_pg["private"],
        )
    finally:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP TABLE {}").format(
                sql.Identifier(probe)).as_string(cursor.connection))
    _admin(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))

    first, second = ("rc_" + uuid.uuid4().hex[:10] for _ in range(2))
    try:
        _admin(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(first)))
        with pytest.raises(backup.OperationsError, match="does not exist"):
            backup.restore_db(run / "db.dump",
                              settings_dict={**connection.settings_dict, "NAME": first})

        _admin(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        _admin(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(second)))
        assert backup.restore_db(
            run / "db.dump", settings_dict={**connection.settings_dict, "NAME": second},
        ) == []
        with psycopg.connect(dbname=second, **_pg()) as restored:
            assert restored.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')", (role, probe)
            ).fetchone()[0] is True
    finally:
        for name in (first, second):
            _admin(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))
        _admin(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
