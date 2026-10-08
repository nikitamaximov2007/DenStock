"""All-or-nothing restore drills on an isolated PostgreSQL 16 with synthetic data."""

import os
import re
import subprocess
import time
import uuid

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from psycopg import sql

from apps.operations import backup, dr_restore
from apps.operations.dr_archive import encrypt_verified_run
from apps.operations.dr_restore import RestoreError, restore_bundle
from tests.emergency_support import configure_test_trust

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]


def _keygen(path):
    out = subprocess.run(
        ["age-keygen", "-o", str(path)], check=True, capture_output=True, text=True,
    )
    return re.search(r"age1[a-z0-9]+", out.stdout + out.stderr).group(0)


@pytest.fixture
def drill(tmp_path, settings):
    if connection.vendor != "postgresql":
        pytest.skip("Isolated PostgreSQL 16 required")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        if int(cursor.fetchone()[0]) // 10000 != 16:
            pytest.skip("Exactly PostgreSQL 16 required")
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-drill"
    settings.DENSTOCK_APP_COMMIT = "a" * 40
    media, private = tmp_path / "media", tmp_path / "private"
    (media / "sub").mkdir(parents=True)
    private.mkdir()
    (media / "sub" / "part.jpg").write_bytes(b"synthetic-public")
    (private / "customer.bin").write_bytes(b"synthetic-private")
    get_user_model().objects.create(username="synthetic-dr-user")
    run = backup.backup_all(
        root=tmp_path / "backups", settings_dict=connection.settings_dict,
        media_root=media, private_media_root=private,
    )
    identity = tmp_path / "age.txt"
    recipient = _keygen(identity)
    bundle = tmp_path / "bundle.tar.age"
    encrypt_verified_run(run, bundle, recipient)
    s = connection.settings_dict
    pg = {"host": s.get("HOST") or "localhost", "port": s.get("PORT") or 5432,
          "user": s.get("USER") or "", "password": s.get("PASSWORD") or ""}
    work = tmp_path / "work"
    work.mkdir()
    return {
        "bundle": bundle, "identity": identity,
        "public": settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH,
        "key_id": settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID, "pg": pg, "work": work,
        "media": tmp_path / "restored" / "media", "private": tmp_path / "restored" / "private",
        "tmp": tmp_path,
    }


def _call(d, **override):
    args = {
        "pg": d["pg"], "new_database": "dr_" + uuid.uuid4().hex[:10],
        "media_target": d["media"], "private_target": d["private"],
        "work_parent": d["work"], "key_id": d["key_id"],
    }
    args.update(override)
    return restore_bundle(d["bundle"], args.pop("identity", d["identity"]),
                          args.pop("public", d["public"]), **args), args["new_database"]


def _database_exists(d, name):
    with psycopg.connect(dbname="postgres", autocommit=True, **d["pg"]) as admin:
        return admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()


def _drop(d, name):
    with psycopg.connect(dbname="postgres", autocommit=True, **d["pg"]) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(name)))


def _assert_clean_failure(d, name):
    assert not _database_exists(d, name)
    assert not d["media"].exists() and not d["private"].exists()
    assert list(d["work"].iterdir()) == []  # no plaintext leftovers
    assert not [p for p in d["media"].parent.glob(".restore-*")]


def test_full_restore_is_verified_and_leaves_no_plaintext(drill):
    started = time.monotonic()
    report, name = _call(drill)
    try:
        assert (drill["media"] / "sub" / "part.jpg").read_bytes() == b"synthetic-public"
        assert (drill["private"] / "customer.bin").read_bytes() == b"synthetic-private"
        with psycopg.connect(dbname=name, **drill["pg"]) as restored:
            assert restored.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE username=%s").format(
                    sql.Identifier(get_user_model()._meta.db_table)),
                ("synthetic-dr-user",),
            ).fetchone()[0] == 1
        assert list(drill["work"].iterdir()) == []
        assert len(report.checks) == 5
        print(f"SYNTHETIC_DR_RESTORE_SECONDS={time.monotonic() - started:.2f}")
    finally:
        _drop(drill, name)


def test_wrong_key_rolls_back_completely(drill):
    other = drill["tmp"] / "other.txt"
    _keygen(other)
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError):
        _call(drill, identity=other, new_database=name)
    _assert_clean_failure(drill, name)


def test_untrusted_signer_rolls_back_completely(drill, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    rogue = tmp_path / "rogue.pem"
    rogue.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError, match="откат"):
        _call(drill, public=rogue, new_database=name)
    _assert_clean_failure(drill, name)


def test_failure_after_database_and_first_directory_rolls_back_everything(drill, monkeypatch):
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(dr_restore.os, "replace", flaky)
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError):
        _call(drill, new_database=name)
    assert len(calls) == 2
    _assert_clean_failure(drill, name)


def test_existing_target_is_never_overwritten(drill):
    drill["private"].mkdir(parents=True)
    (drill["private"] / "keep.txt").write_text("precious")
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError, match="перезапись запрещена"):
        _call(drill, new_database=name)
    assert (drill["private"] / "keep.txt").read_text() == "precious"
    assert not _database_exists(drill, name)
