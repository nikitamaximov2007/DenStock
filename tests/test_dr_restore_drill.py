"""Unit coverage for the independent offsite DR selection guard."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = Path(__file__).parents[1] / "scripts" / "operations" / "dr_restore_drill.py"
SPEC = importlib.util.spec_from_file_location("dr_restore_drill", SCRIPT)
assert SPEC and SPEC.loader
dr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dr
SPEC.loader.exec_module(dr)


class LocalReadOnlyStore:
    def __init__(self, runs: dict[str, dict[str, bytes]]):
        self.runs = runs
        self.calls: list[tuple[str, str]] = []

    def list_prefixes(self, prefix):
        self.calls.append(("LIST", prefix))
        return [f"{prefix}{name}/" for name in self.runs]

    def list_objects(self, prefix):
        self.calls.append(("LIST", prefix))
        return list(self.runs[prefix.rstrip("/").split("/")[-1]])

    def read_text(self, key):
        self.calls.append(("GET", key))
        run, name = key.rstrip("/").split("/")[-2:]
        return self.runs[run][name].decode()


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "trusted.pub"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    monkeypatch.setattr(dr, "EXPECTED_FINGERPRINT", dr.public_key_fingerprint(public_path))
    return private, public_path


def signed_manifest(private, *, source="production", key_id="production-1", valid_hash=True):
    database = b"database"
    media = b"media"
    manifest = {
        "schema_version": 2,
        "backup_run_id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "source_environment": source,
        "app_commit": "a" * 40,
        "database_dump_filename": "db.dump",
        "database_sha256": dr.hashlib.sha256(database).hexdigest() if valid_hash else "0" * 64,
        "media_filename": "media.tar.gz",
        "media_sha256": dr.hashlib.sha256(media).hexdigest(),
        "migration_state": [["core", "0001_initial"]],
        "migration_fingerprint": "1" * 64,
    }
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "value": base64.b64encode(private.sign(dr.canonical_manifest_payload(manifest))).decode(),
    }
    return manifest, database, media


def run_payload(manifest, database=b"database", media=b"media"):
    return {
        "manifest.json": json.dumps(manifest).encode(),
        "db.dump": database,
        "media.tar.gz": media,
    }


def test_valid_manifest_verifies(signing_key):
    private, public = signing_key
    manifest, _, _ = signed_manifest(private)
    dr.verify_manifest(manifest, public, "production-1")


@pytest.mark.parametrize(
    "field,value", [("source_environment", "test"), ("signature", {}), ("app_commit", "short")]
)
def test_invalid_manifest_fails_closed(signing_key, field, value):
    private, public = signing_key
    manifest, _, _ = signed_manifest(private)
    manifest[field] = value
    with pytest.raises(dr.DrillError):
        dr.verify_manifest(manifest, public, "production-1")


def test_wrong_key_id_and_bad_signature_are_rejected(signing_key):
    private, public = signing_key
    manifest, _, _ = signed_manifest(private, key_id="other")
    with pytest.raises(dr.DrillError, match="key ID"):
        dr.verify_manifest(manifest, public, "production-1")
    manifest, _, _ = signed_manifest(private)
    manifest["signature"]["value"] = base64.b64encode(b"bad").decode()
    with pytest.raises(dr.DrillError, match="signature"):
        dr.verify_manifest(manifest, public, "production-1")


def test_newest_incomplete_backup_is_skipped(signing_key):
    private, public = signing_key
    older, database, media = signed_manifest(private)
    newer, _, _ = signed_manifest(private)
    store = LocalReadOnlyStore(
        {
            "2026-01-01": run_payload(older, database, media),
            "2026-01-02": {"manifest.json": json.dumps(newer).encode()},
        }
    )
    selected, reasons = dr.select_latest_verified_backup(store, "", public, "production-1")
    assert selected.backup_id == "2026-01-01"
    assert "incomplete" in reasons[0]
    assert not any(call[0] in {"PUT", "POST", "DELETE"} for call in store.calls)


def test_wrong_public_fingerprint_is_rejected(signing_key, monkeypatch):
    private, public = signing_key
    manifest, _, _ = signed_manifest(private)
    monkeypatch.setattr(dr, "EXPECTED_FINGERPRINT", "0" * 64)
    with pytest.raises(dr.DrillError, match="fingerprint"):
        dr.verify_manifest(manifest, public, "production-1")


def test_production_and_unmarked_targets_are_refused(tmp_path):
    with pytest.raises(dr.DrillError):
        dr.ensure_safe_target(Path("/opt/denstock/dr"), execute=False)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "foreign.txt").write_text("do not touch")
    with pytest.raises(dr.DrillError, match="Non-empty"):
        dr.ensure_safe_target(occupied, execute=False)


def test_missing_credentials_fail_closed(signing_key, tmp_path, monkeypatch):
    _, public = signing_key
    monkeypatch.delenv("DENSTOCK_DR_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("DENSTOCK_DR_S3_SECRET_KEY", raising=False)
    assert (
        dr.main(
            [
                "--bucket",
                "denstock-backups-nikita",
                "--endpoint",
                "https://storage.yandexcloud.net",
                "--work-dir",
                str(tmp_path / "dr"),
                "--public-key",
                str(public),
            ]
        )
        == 2
    )


def test_hash_mismatch_is_detectable(tmp_path):
    payload = tmp_path / "payload"
    payload.write_bytes(b"unexpected")
    assert dr.sha256_file(payload) != dr.hashlib.sha256(b"expected").hexdigest()


def test_s3_listing_uses_sigv4_sorted_query_and_empty_root_prefix(monkeypatch):
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/" />'

    def fake_urlopen(request, timeout):
        captured.append(request.full_url)
        return Response()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    store = dr.S3ReadOnlyStore(
        "https://storage.yandexcloud.net", "bucket", "access", "secret", "ru-central1"
    )
    assert store.list_prefixes("") == []
    query = urlsplit(captured[0]).query
    assert query == "delimiter=%2F&list-type=2&prefix="
    assert dict(parse_qsl(query, keep_blank_values=True)) == {
        "delimiter": "/",
        "list-type": "2",
        "prefix": "",
    }


def test_local_command_start_failure_is_reported_as_drill_error(tmp_path, monkeypatch):
    def missing_command(*args, **kwargs):
        raise FileNotFoundError("docker is unavailable")

    monkeypatch.setattr(dr.subprocess, "run", missing_command)
    with pytest.raises(dr.DrillError, match="could not start"):
        dr.run_checked(["docker", "compose", "ps"], cwd=tmp_path)


def test_dr_compose_override_bypasses_entrypoint_migrations(tmp_path):
    override = dr.write_dr_compose_override(tmp_path)
    content = override.read_text(encoding="utf-8")
    assert "entrypoint: []" in content
    assert '"gunicorn"' in content


def test_disposable_env_sets_the_signed_application_commit(tmp_path):
    dr.write_disposable_env(tmp_path, "2026-09-02_03-00-47", "a" * 40)
    assert f"DENSTOCK_APP_COMMIT={'a' * 40}" in (tmp_path / ".env").read_text(encoding="utf-8")
