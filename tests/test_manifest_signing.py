import copy
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.operations.manifest_signing import (
    ManifestSignatureError,
    canonical_manifest_payload,
    sign_manifest,
    verify_manifest,
)


def _keys(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def _manifest():
    return {
        "schema_version": 2,
        "database_identity": str(uuid.uuid4()),
        "backup_run_id": str(uuid.uuid4()),
        "created_at": "2026-08-18T12:00:00+00:00",
        "database_sha256": "a" * 64,
        "media_sha256": "b" * 64,
        "app_commit": "c" * 40,
        "migration_fingerprint": "d" * 64,
        "data_state": {"business_generation": 1, "business_sha256": "e" * 64},
        "authorized_emergency_primary_id": str(uuid.uuid4()),
        "primary_authorization_epoch": 1,
    }


@pytest.fixture
def signing_settings(tmp_path, settings):
    private, public = _keys(tmp_path)
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_MANIFEST_SIGNING_KEY_PATH = str(private)
    settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH = str(public)
    settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID = "production-1"
    return settings


def test_signed_manifest_is_deterministic_and_verifies(signing_settings):
    manifest = _manifest()
    canonical = canonical_manifest_payload(manifest)
    sign_manifest(manifest)
    verify_manifest(manifest)
    assert canonical == canonical_manifest_payload(manifest)


@pytest.mark.parametrize(
    "path,value",
    [
        ("authorized_emergency_primary_id", str(uuid.uuid4())),
        ("primary_authorization_epoch", 2),
        ("app_commit", "f" * 40),
        ("database_sha256", "f" * 64),
        ("media_sha256", "f" * 64),
    ],
)
def test_signed_manifest_rejects_tampering(signing_settings, path, value):
    manifest = _manifest()
    sign_manifest(manifest)
    manifest[path] = value
    with pytest.raises(ManifestSignatureError):
        verify_manifest(manifest)


def test_missing_or_wrong_key_signature_is_rejected(signing_settings, tmp_path):
    manifest = _manifest()
    sign_manifest(manifest)
    unsigned = copy.deepcopy(manifest)
    unsigned.pop("signature")
    with pytest.raises(ManifestSignatureError):
        verify_manifest(unsigned)
    _, wrong_public = _keys(tmp_path / "wrong")
    signing_settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH = str(wrong_public)
    with pytest.raises(ManifestSignatureError):
        verify_manifest(manifest)
