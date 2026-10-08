import base64
import json
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.operations.backup_budget import BudgetError
from apps.operations.dr_local import LocalEncryptedStore


def _source(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    return path, sha256(content).hexdigest()


def _keys(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = tmp_path / "public.pem"
    public.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return key, public


def _receipt(key, name, content):
    receipt = {
        "version": 1, "run": name, "bytes": len(content),
        "sha256": sha256(content).hexdigest(),
    }
    payload = json.dumps(receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    receipt["signature"] = {
        "algorithm": "ed25519", "key_id": "production-1",
        "value": base64.b64encode(key.sign(payload.encode("ascii"))).decode("ascii"),
    }
    return receipt


def test_local_install_verifies_and_rotates_oldest(tmp_path):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public, limit=900)
    for name in ("generation1", "generation2", "generation3"):
        source, digest = _source(tmp_path, name, b"x" * 100)
        assert digest == _receipt(key, name, b"x" * 100)["sha256"]
        store.install(name, source, _receipt(key, name, b"x" * 100))
    assert store.physical_bytes() <= 900
    assert store.is_verified("generation3")
    assert len([g for g in store.generations() if g.verified]) >= 1


def test_interrupted_generation_counts_and_does_not_become_verified(tmp_path):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public, limit=500)
    incomplete = store.root / "interrupted1"
    incomplete.mkdir()
    (incomplete / "bundle.tar.age").write_bytes(b"x" * 250)
    source, _ = _source(tmp_path, "new", b"y" * 100)
    with pytest.raises(BudgetError, match="Нет проверенного"):
        store.install("generation1", source, _receipt(key, "generation1", b"y" * 100))
    assert not store.is_verified("interrupted1")
    assert store.physical_bytes() == 250


def test_bad_cipher_is_never_installed(tmp_path):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public)
    source, _ = _source(tmp_path, "bad", b"not-expected")
    with pytest.raises(BudgetError, match="Контрольная сумма"):
        store.install("generation1", source, _receipt(key, "generation1", b"expected"))
    assert not store.generations()
