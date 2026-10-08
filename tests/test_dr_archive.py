import json
import sqlite3
import subprocess
import tarfile
import uuid

import pytest

from apps.operations import backup
from apps.operations.dr_archive import (
    ArchiveError,
    encrypt_verified_run,
    sha256_file,
    signed_cipher_receipt,
)
from apps.operations.dr_local import verify_receipt
from apps.operations.emergency_manifest import validate_manifest
from apps.operations.manifest_signing import verify_manifest
from tests.emergency_support import configure_test_trust


@pytest.mark.django_db
def test_age_bundle_round_trip_contains_signed_db_and_both_media(tmp_path, settings):
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-production"
    settings.DENSTOCK_APP_COMMIT = "a" * 40
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.PRIVATE_MEDIA_ROOT.mkdir()
    (settings.PRIVATE_MEDIA_ROOT / "request.bin").write_bytes(b"synthetic private file")
    media = tmp_path / "media"
    media.mkdir()
    (media / "part.jpg").write_bytes(b"synthetic ordinary file")
    database = tmp_path / "sample.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE synthetic (value TEXT)")
        conn.execute("INSERT INTO synthetic VALUES ('restored')")

    run = backup.backup_all(
        root=tmp_path / "backups",
        settings_dict={"ENGINE": "django.db.backends.sqlite3", "NAME": str(database)},
        media_root=media,
    )
    secret = tmp_path / "age-key.txt"
    generated = subprocess.run(
        ["age-keygen", "-o", str(secret)], capture_output=True, text=True, check=True,
    )
    recipient = next(
        line.split(": ", 1)[1]
        for line in (generated.stdout + generated.stderr).splitlines()
        if line.startswith("Public key: ")
    )
    encrypted = tmp_path / "bundle.tar.age"
    size, digest = encrypt_verified_run(run, encrypted, recipient)
    assert size == encrypted.stat().st_size
    assert digest == sha256_file(encrypted)
    assert verify_receipt(
        signed_cipher_receipt("generation1", encrypted),
        settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH,
        settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID,
    )
    assert b"synthetic private file" not in encrypted.read_bytes()

    restored_bundle = tmp_path / "bundle.tar"
    subprocess.run(
        ["age", "-d", "-i", str(secret), "-o", str(restored_bundle), str(encrypted)],
        check=True, capture_output=True,
    )
    restored = tmp_path / "restored"
    restored.mkdir()
    with tarfile.open(restored_bundle) as archive:
        archive.extractall(restored, filter="data")
    manifest = json.loads((restored / "manifest.json").read_text())
    verify_manifest(manifest)
    assert validate_manifest(restored).ok
    with tarfile.open(restored / "private_media.tar.gz") as archive:
        assert archive.extractfile("./request.bin").read() == b"synthetic private file"
    with tarfile.open(restored / "media.tar.gz") as archive:
        assert archive.extractfile("./part.jpg").read() == b"synthetic ordinary file"
    with pytest.raises(ArchiveError, match="уже существует"):
        encrypt_verified_run(run, encrypted, recipient)
