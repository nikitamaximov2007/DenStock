"""Ed25519 authenticity for emergency manifests."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from django.conf import settings


class ManifestSignatureError(ValueError):
    pass


def canonical_manifest_payload(manifest: dict) -> bytes:
    payload = dict(manifest)
    payload.pop("signature", None)
    try:
        return json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ManifestSignatureError("Manifest cannot be canonicalized.") from exc


def sign_manifest(manifest: dict) -> None:
    if settings.DENSTOCK_MODE != "production":
        raise ManifestSignatureError("Only production may sign a manifest.")
    path = Path(settings.DENSTOCK_MANIFEST_SIGNING_KEY_PATH)
    key_id = settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID
    if not path.is_file() or not key_id:
        raise ManifestSignatureError("Production manifest signing key is not configured.")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ManifestSignatureError("Manifest signing key must be Ed25519.")
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "value": base64.b64encode(key.sign(canonical_manifest_payload(manifest))).decode("ascii"),
    }


def verify_manifest(manifest: dict) -> None:
    signature = manifest.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        raise ManifestSignatureError("Manifest signature is missing or unsupported.")
    if signature.get("key_id") != settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID:
        raise ManifestSignatureError("Manifest signing key is not trusted.")
    path = Path(settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH)
    if not path.is_file():
        raise ManifestSignatureError("Pinned manifest public key is unavailable.")
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
        value = base64.b64decode(signature["value"], validate=True)
        if not isinstance(key, Ed25519PublicKey):
            raise TypeError
        key.verify(value, canonical_manifest_payload(manifest))
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise ManifestSignatureError("Manifest signature is invalid.") from exc
