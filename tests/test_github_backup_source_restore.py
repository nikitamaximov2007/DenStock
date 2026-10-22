"""The recovery kit must recover the signed source SHA from a fresh Git clone."""

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "scripts/dr/prepare_kit_restore.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("age") and shutil.which("age-keygen")), reason="age is required"
)


def _run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def _git_repository(root):
    source = root / "source"
    source.mkdir()
    _run("git", "init", "-q", "-b", "main", str(source))
    _run("git", "-C", str(source), "config", "user.name", "Recovery Test")
    _run("git", "-C", str(source), "config", "user.email", "recovery@example.invalid")
    (source / "README.md").write_text("main\n")
    _run("git", "-C", str(source), "add", "README.md")
    _run("git", "-C", str(source), "commit", "-q", "-m", "main")
    _run("git", "-C", str(source), "checkout", "-q", "-b", "production")
    (source / "production.txt").write_text("exact deployed source\n")
    _run("git", "-C", str(source), "add", "production.txt")
    _run("git", "-C", str(source), "commit", "-q", "-m", "production")
    commit = _run("git", "-C", str(source), "rev-parse", "HEAD")
    _run("git", "-C", str(source), "checkout", "-q", "main")
    bare = root / "remote.git"
    _run("git", "clone", "-q", "--bare", str(source), str(bare))
    shallow = root / "shallow-main"
    _run("git", "clone", "-q", "--depth", "1", "--single-branch", "--branch", "main",
         bare.as_uri(), str(shallow))
    return bare, shallow, commit


def _kit(root, commit):
    identity = root / "identity.txt"
    _run("age-keygen", "-o", str(identity))
    recipient = _run("age-keygen", "-y", str(identity))
    run = root / "run"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({
        "app_commit": commit, "created_at": "2026-10-22T00:00:00+00:00",
        "signature": {"key_id": "synthetic"},
    }))
    (run / "private_media.tar.gz").write_bytes(b"synthetic private media")
    archive = root / "run.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(run, arcname="run")
    kit = root / "kit"
    kit.mkdir()
    _run("age", "-r", recipient, "-o", str(kit / "synthetic.signed-backup.tar.age"),
         str(archive))
    pubkey = root / "pinned-public.pem"
    pubkey.write_text("synthetic public key")
    return kit, identity, pubkey


def _prepare(root, kit, identity, pubkey, source, name):
    return subprocess.run([
        "bash", str(PREPARE), "--kit", str(kit), "--identity", str(identity),
        "--work", str(root / name), "--plain-identity", "--fresh-secrets",
        "--pubkey", str(pubkey), "--source-git", str(source),
    ], capture_output=True, text=True, timeout=120)


def test_full_clone_recovers_exact_non_main_source_sha_and_fresh_secrets(tmp_path):
    bare, _, commit = _git_repository(tmp_path)
    kit, identity, pubkey = _kit(tmp_path, commit)
    result = _prepare(tmp_path, kit, identity, pubkey, bare, "restored")
    assert result.returncode == 0, result.stdout + result.stderr
    restored = tmp_path / "restored"
    assert _run("git", "-C", str(restored / "source"), "rev-parse", "HEAD") == commit
    assert (restored / "source/production.txt").read_text() == "exact deployed source\n"
    assert (restored / "env/db.env").is_file()
    assert not (restored / "secrets/opt/denstock/.env").exists()


def test_shallow_single_branch_source_refuses_missing_signed_sha(tmp_path):
    _, shallow, commit = _git_repository(tmp_path)
    kit, identity, pubkey = _kit(tmp_path, commit)
    result = _prepare(tmp_path, kit, identity, pubkey, shallow, "missing")
    assert result.returncode != 0
    assert "source Git SHA is absent" in result.stdout + result.stderr
    assert not (tmp_path / "missing/env/db.env").exists()
