"""Production-shaped chain on a real PostgreSQL 16 with synthetic data.

database + public media + private media -> backup -> signed manifest -> verify
-> restore into a NEW database and NEW directories -> compare content.
"""

import hashlib
import json
import subprocess
import uuid

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from psycopg import sql

from apps.operations import backup, restore
from apps.operations.emergency_manifest import validate_manifest
from apps.operations.manifest_signing import verify_manifest
from apps.operations.private_media import Inventory, PrivateMediaError, restore_archive
from tests.emergency_support import configure_test_trust

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]


def _tree(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_full_chain_restores_database_public_and_private_media(tmp_path, settings):
    if connection.vendor != "postgresql":
        pytest.skip("Isolated PostgreSQL 16 required")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        if int(cursor.fetchone()[0]) // 10000 != 16:
            pytest.skip("Exactly PostgreSQL 16 required")
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-pg16"
    settings.DENSTOCK_APP_COMMIT = "d" * 40
    settings.BACKUP_ROOT = tmp_path / "backups"
    media, private = tmp_path / "media", tmp_path / "private"
    (media / "parts").mkdir(parents=True)
    (private / "customer_requests" / "7").mkdir(parents=True)
    (private / "catalog-imports").mkdir()
    (media / "parts" / "photo.jpg").write_bytes(b"synthetic public photo")
    (private / "customer_requests" / "7" / "scan.pdf").write_bytes(b"synthetic private scan")
    (private / "catalog-imports" / "import.bin").write_bytes(bytes(range(256)) * 400)
    settings.PRIVATE_MEDIA_ROOT = private
    get_user_model().objects.create(username="synthetic-pg16-user")

    run = backup.backup_all(
        settings_dict=connection.settings_dict, media_root=media, private_media_root=private,
    )
    manifest = json.loads((run / "manifest.json").read_text())
    verify_manifest(manifest)
    assert validate_manifest(run, expected_source="production").ok
    report = restore.verify_backup(run.name)
    assert report.ok, report.errors
    assert report.private_media_file == "private_media.tar.gz"

    s = connection.settings_dict
    pg = {"host": s.get("HOST") or "localhost", "port": s.get("PORT") or 5432,
          "user": s.get("USER") or "", "password": s.get("PASSWORD") or ""}
    target_db = "pm_" + uuid.uuid4().hex[:10]
    with psycopg.connect(dbname="postgres", autocommit=True, **pg) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
    try:
        subprocess.run(
            ["pg_restore", "--no-owner", "--no-acl", "--exit-on-error",
             "-h", str(pg["host"]), "-p", str(pg["port"]), "-U", pg["user"],
             "-d", target_db, str(run / manifest["database_dump_filename"])],
            check=True, capture_output=True,
        )
        with psycopg.connect(dbname=target_db, **pg) as restored:
            count = restored.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE username=%s").format(
                    sql.Identifier(get_user_model()._meta.db_table)),
                ("synthetic-pg16-user",),
            ).fetchone()[0]
        assert count == 1
    finally:
        with psycopg.connect(dbname="postgres", autocommit=True, **pg) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(target_db)))

    restored_media = tmp_path / "restored-media"
    backup.restore_media(run / "media.tar.gz", media_root=restored_media)
    assert _tree(restored_media) == _tree(media)
    expected = Inventory(manifest["private_media_file_count"], manifest["private_media_bytes"],
                         manifest["private_media_tree_sha256"])
    restored_private = tmp_path / "restored-private"
    restore_archive(run / "private_media.tar.gz", restored_private, expected=expected)
    assert _tree(restored_private) == _tree(private)

    # Tampering is detected and a failed restore leaves the previous state.
    archive = run / "private_media.tar.gz"
    data = bytearray(archive.read_bytes())
    data[len(data) // 3] ^= 0x01
    archive.write_bytes(bytes(data))
    assert not restore.verify_backup(run.name).ok
    before = _tree(restored_private)
    with pytest.raises(PrivateMediaError):
        restore_archive(archive, restored_private, expected=expected)
    assert _tree(restored_private) == before
