"""Shared real Ed25519 trust fixtures for emergency tests."""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.operations.manifest_signing import sign_manifest


def configure_test_trust(tmp_path, settings, *, workstation_id):
    private = Ed25519PrivateKey.generate()
    private_path = tmp_path / "production-signing-private.pem"
    public_path = tmp_path / "pinned-production-public.pem"
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
    settings.DENSTOCK_MANIFEST_SIGNING_KEY_PATH = str(private_path)
    settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH = str(public_path)
    settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID = "test-production-1"
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID = str(workstation_id)


def sign_production_manifest(manifest, settings):
    mode = settings.DENSTOCK_MODE
    settings.DENSTOCK_MODE = "production"
    try:
        sign_manifest(manifest)
    finally:
        settings.DENSTOCK_MODE = mode
    return manifest
