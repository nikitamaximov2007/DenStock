import base64
import json
from datetime import UTC, datetime
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


def _receipt(key, name, content, *, created_at=None):
    receipt = {
        "version": 1, "run": name, "bytes": len(content),
        "sha256": sha256(content).hexdigest(),
        "backup_created_at": created_at or datetime.now(UTC).isoformat(),
    }
    payload = json.dumps(receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    receipt["signature"] = {
        "algorithm": "ed25519", "key_id": "production-1",
        "value": base64.b64encode(key.sign(payload.encode("ascii"))).decode("ascii"),
    }
    return receipt


def _at(hour):
    return f"2026-10-0{hour}T03:00:00+00:00"


def test_local_rotation_removes_older_only_after_newer_is_verified(tmp_path):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public, limit=1100)
    for day, name in enumerate(("generation1", "generation2", "generation3"), start=1):
        source, _ = _source(tmp_path, name, b"x" * 100)
        store.install(name, source, _receipt(key, name, b"x" * 100, created_at=_at(day)))
        assert store.physical_bytes() <= 1100
    assert [g.name for g in store.generations() if g.verified] == ["generation3"]


def test_local_install_fails_closed_when_old_and_new_cannot_coexist(tmp_path):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public, limit=700)
    source, _ = _source(tmp_path, "generation1", b"x" * 100)
    store.install("generation1", source, _receipt(key, "generation1", b"x" * 100,
                                                  created_at=_at(1)))
    source, _ = _source(tmp_path, "generation2", b"x" * 100)
    with pytest.raises(BudgetError, match="не помещается"):
        store.install("generation2", source, _receipt(key, "generation2", b"x" * 100,
                                                      created_at=_at(2)))
    assert store.is_verified("generation1")  # the last good copy was never sacrificed
    assert not (store.root / "generation2").exists()


def test_older_copy_is_never_pruned_by_an_older_or_undated_newcomer(tmp_path):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public, limit=5000)
    source, _ = _source(tmp_path, "generation2", b"x" * 100)
    store.install("generation2", source, _receipt(key, "generation2", b"x" * 100,
                                                  created_at=_at(2)))
    source, _ = _source(tmp_path, "generation1", b"y" * 100)
    store.install("generation1", source, _receipt(key, "generation1", b"y" * 100,
                                                  created_at=_at(1)))
    assert store.is_verified("generation1") and store.is_verified("generation2")


def test_interrupted_generation_counts_and_does_not_become_verified(tmp_path):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public, limit=500)
    incomplete = store.root / "interrupted1"
    incomplete.mkdir()
    (incomplete / "bundle.tar.age").write_bytes(b"x" * 250)
    source, _ = _source(tmp_path, "new", b"y" * 100)
    with pytest.raises(BudgetError, match="не помещается"):
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
