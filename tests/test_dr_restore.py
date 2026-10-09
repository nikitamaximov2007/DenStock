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
        "tmp": tmp_path, "run": run, "recipient": recipient,
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


def test_damaged_ciphertext_rolls_back_completely(drill):
    damaged = drill["tmp"] / "damaged.age"
    damaged.write_bytes(drill["bundle"].read_bytes()[:20])
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError):
        restore_bundle(
            damaged, drill["identity"], drill["public"], pg=drill["pg"],
            new_database=name, media_target=drill["media"],
            private_target=drill["private"], work_parent=drill["work"],
            key_id=drill["key_id"],
        )
    _assert_clean_failure(drill, name)


def test_pg_restore_failure_rolls_back_completely(drill, monkeypatch):
    original = dr_restore.subprocess.run

    def failed_restore(args, **kwargs):
        if args[0] == "pg_restore":
            raise subprocess.CalledProcessError(1, args)
        return original(args, **kwargs)

    monkeypatch.setattr(dr_restore.subprocess, "run", failed_restore)
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError):
        _call(drill, new_database=name)
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


def test_bit_flipped_ciphertext_rolls_back_completely(drill):
    data = bytearray(drill["bundle"].read_bytes())
    data[len(data) // 2] ^= 0x01  # one flipped bit deep inside the payload
    flipped = drill["tmp"] / "flipped.age"
    flipped.write_bytes(bytes(data))
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError):
        restore_bundle(
            flipped, drill["identity"], drill["public"], pg=drill["pg"],
            new_database=name, media_target=drill["media"],
            private_target=drill["private"], work_parent=drill["work"],
            key_id=drill["key_id"],
        )
    _assert_clean_failure(drill, name)


def test_media_extraction_failing_midway_rolls_back_completely(drill, monkeypatch):
    import tarfile

    real = tarfile.TarFile.extractall
    calls = []

    def partial_then_fail(self, path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 2:  # private_media: write something, then fail
            (dr_restore.Path(path) / "half-written.bin").write_bytes(b"partial")
            raise tarfile.ExtractError("disk error")
        return real(self, path, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extractall", partial_then_fail)
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError):
        _call(drill, new_database=name)
    assert len(calls) == 2
    _assert_clean_failure(drill, name)


def test_old_format_without_private_media_is_refused_not_half_restored(drill, settings):
    """A historical Yandex-format run has no private_media.  It stays restorable by
    the old db/media commands, but the DR restore must not pretend it is complete."""
    import json
    import tarfile

    from apps.operations.manifest_signing import sign_manifest

    run = drill["run"]
    manifest = json.loads((run / "manifest.json").read_text())
    for field in ("private_media_filename", "private_media_sha256",
                  "private_media_tree_sha256", "signature"):
        manifest.pop(field, None)
    manifest["sha256"].pop("private_media.tar.gz")
    sign_manifest(manifest)
    legacy = drill["tmp"] / "legacy"
    legacy.mkdir()
    (legacy / "manifest.json").write_text(json.dumps(manifest))
    package = drill["tmp"] / "legacy.tar"
    with tarfile.open(package, "w") as out:
        for item in ("manifest.json", "db.dump", "media.tar.gz"):
            source = legacy / item if item == "manifest.json" else run / item
            out.add(source, arcname=item)
    bundle = drill["tmp"] / "legacy.age"
    subprocess.run(["age", "-r", drill["recipient"], "-o", str(bundle), str(package)],
                   check=True)
    name = "dr_" + uuid.uuid4().hex[:10]
    with pytest.raises(RestoreError):
        restore_bundle(
            bundle, drill["identity"], drill["public"], pg=drill["pg"],
            new_database=name, media_target=drill["media"],
            private_target=drill["private"], work_parent=drill["work"],
            key_id=drill["key_id"],
        )
    _assert_clean_failure(drill, name)
