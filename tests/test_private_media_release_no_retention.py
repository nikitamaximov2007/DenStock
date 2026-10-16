"""Synthetic safety test for the release-only, no-retention operator procedure.

The fakes fail on any rclone operation that could delete an existing object.
No production data or cloud credentials are involved.
"""

import fcntl
import hashlib
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.operations import backup
from tests.emergency_support import configure_test_trust

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts/operations/private_media_release_backup_no_retention.sh"
)

FAKE_DOCKER = r'''
import base64, hashlib, io, json, os, sys, tarfile
from pathlib import Path
from cryptography.hazmat.primitives import serialization

root = Path(os.environ["FAKE_ROOT"])
args = sys.argv[1:]
if "du" in args:
    print("1000\t/app/private_media")
elif "backup_all" in args:
    run = root / "backups" / os.environ["FAKE_RUN_ID"]
    run.mkdir()
    (run / "db.dump").write_bytes(b"synthetic-pg-dump")
    names = ["media.tar.gz"]
    if os.environ["FAKE_PHASE"] == "post":
        names.append("private_media.tar.gz")
    for name in names:
        with tarfile.open(run / name, "w:gz") as tar:
            data = b"synthetic-file"
            info = tarfile.TarInfo("example.bin")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()}
    manifest = {
        "app_commit": os.environ["FAKE_EXPECTED_SHA"],
        "sha256": checksums,
        "private_media_status": "included" if os.environ["FAKE_PHASE"] == "post" else "absent",
    }
    key = serialization.load_pem_private_key(
        Path(os.environ["FAKE_SIGNING_KEY"]).read_bytes(), password=None
    )
    payload = json.dumps(
        manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    manifest["signature"] = {
        "algorithm": "ed25519", "key_id": "production-1",
        "value": base64.b64encode(key.sign(payload)).decode("ascii"),
    }
    if os.environ.get("FAKE_BAD_SIGNATURE") == "1":
        manifest["app_commit"] = "0" * 40
    (run / "manifest.json").write_text(json.dumps(manifest))
elif "verify_backup" in args or "shell" in args:
    run = root / "backups" / os.environ["FAKE_RUN_ID"]
    manifest = json.loads((run / "manifest.json").read_text())
    sig = manifest.pop("signature")
    payload = json.dumps(
        manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    pub = serialization.load_pem_public_key(Path(os.environ["FAKE_PUBLIC_KEY"]).read_bytes())
    pub.verify(base64.b64decode(sig["value"]), payload)
    assert sig["key_id"] == "production-1"
    assert all(
        hashlib.sha256((run / name).read_bytes()).hexdigest() == value
        for name, value in manifest["sha256"].items()
    )
    expected_private = "included" if os.environ["FAKE_PHASE"] == "post" else "absent"
    assert manifest["private_media_status"] == expected_private
else:
    raise SystemExit("unexpected docker call")
'''

FAKE_RCLONE = r'''
import hashlib, json, os, shutil, sys
from pathlib import Path

remote = Path(os.environ["FAKE_REMOTE_ROOT"])
args = sys.argv[1:]
mode = os.environ.get("FAKE_RCLONE_MODE", "")
def path(value):
    if value.startswith("remote:denstock"):
        return remote / value.removeprefix("remote:denstock").lstrip("/")
    return Path(value)
def inventory(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*") if p.is_file()}
if args[:2] == ["backend", "list-multipart-uploads"]:
    uploads = [{"UploadId": "synthetic"}] if os.environ.get("FAKE_RCLONE_MPU") == "1" else []
    print(json.dumps({"denstock": uploads}))
elif args[0] == "size":
    folder = path(args[-2] if args[-1] == "--json" else args[-1])
    files = [p for p in folder.rglob("*") if p.is_file()]
    extra = int(os.environ.get("FAKE_REMOTE_EXTRA_BYTES", "0")) if folder == remote else 0
    print(json.dumps({"bytes": sum(p.stat().st_size for p in files) + extra, "count": len(files)}))
elif args[0] == "lsf":
    if mode == "listing_error": raise SystemExit(19)
    for p in sorted(remote.iterdir()):
        if p.is_dir(): print(p.name + "/")
elif args[:2] == ["copy", "--immutable"]:
    source, dest = path(args[2]), path(args[3])
    dest.mkdir(parents=True, exist_ok=False)
    files = sorted(p for p in source.rglob("*") if p.is_file())
    for idx, p in enumerate(files):
        target = dest / p.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        if mode == "interrupt" and idx == 0: raise SystemExit(17)
    if mode == "corrupt":
        target = dest / files[0].relative_to(source)
        target.write_bytes(target.read_bytes() + b"corrupt")
elif args[:2] == ["check", "--download"]:
    source, dest = path(args[2]), path(args[3])
    if inventory(source) != inventory(dest): raise SystemExit(18)
else:
    raise SystemExit("forbidden or unexpected rclone operation: " + args[0])
'''

FAKE_FLOCK = r'''
import fcntl, sys
assert sys.argv[1] == "-n"
try:
    fcntl.flock(int(sys.argv[2]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
'''


def _inventory(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*") if p.is_file()
    }


@pytest.fixture
def synthetic(tmp_path):
    root, remote, bin_dir = (tmp_path / name for name in ("app", "remote", "bin"))
    for path in (root / "backups", remote, bin_dir):
        path.mkdir(parents=True)
    (root / ".env.backup").write_text(
        "BACKUP_OFFSITE_ENABLED=true\nBACKUP_OFFSITE_METHOD=rclone\n"
        "BACKUP_OFFSITE_TARGET=remote:denstock\n"
    )
    for idx in range(31):
        name = f"2026-09-{idx + 1:02d}_03-00-00"
        run = root / "backups" / name
        run.mkdir()
        (run / "db.dump").write_bytes(f"old-{idx}".encode())
        if idx < 22:
            (run / "manifest.json").write_text('{"verification_status":"verified"}')
    old_remote = remote / "old-verified"
    old_remote.mkdir()
    (old_remote / "manifest.json").write_bytes(b"old-remote-verified")
    private = Ed25519PrivateKey.generate()
    key_path, pub_path = tmp_path / "test-private.pem", tmp_path / "test-public.pem"
    key_path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    pub_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    for name, source in (("docker", FAKE_DOCKER), ("rclone", FAKE_RCLONE), ("flock", FAKE_FLOCK)):
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o700)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "DENSTOCK_RELEASE_SYNTHETIC": "1",
        "DENSTOCK_RELEASE_TEST_ROOT": str(root),
        "DENSTOCK_RELEASE_TEST_LOCK": str(tmp_path / "backup.lock"),
        "FAKE_ROOT": str(root),
        "FAKE_REMOTE_ROOT": str(remote),
        "FAKE_SIGNING_KEY": str(key_path),
        "FAKE_PUBLIC_KEY": str(pub_path),
    })
    return root, remote, env


def _run(env, phase, run_id, **extra):
    values = {**env, **extra, "FAKE_PHASE": phase, "FAKE_RUN_ID": run_id}
    values["FAKE_EXPECTED_SHA"] = (
        "f7b80861fc1d81f6be70c86e78be7f06418a82d2" if phase == "pre"
        else "e35948804069b3fe8ffa3f499fefe0a775541b47"
    )
    return subprocess.run(
        ["bash", "-s", "--", phase], input=SCRIPT.read_text(), env=values,
        capture_output=True, text=True,
    )


def test_pre_post_keep_22_complete_and_9_partial_and_remote_objects(synthetic):
    root, remote, env = synthetic
    local_before, remote_before = _inventory(root / "backups"), _inventory(remote)
    pre = _run(env, "pre", "2026-10-16_12-00-00")
    assert pre.returncode == 0, pre.stderr
    post = _run(env, "post", "2026-10-16_12-01-00")
    assert post.returncode == 0, post.stderr
    assert len([p for p in (root / "backups").iterdir() if p.is_dir()]) == 33
    after_local, after_remote = _inventory(root / "backups"), _inventory(remote)
    assert all(after_local[name] == digest for name, digest in local_before.items())
    assert all(after_remote[name] == digest for name, digest in remote_before.items())
    assert len([p for p in remote.iterdir() if p.is_dir()]) == 3


@pytest.mark.parametrize("mode", ["interrupt", "corrupt"])
def test_failed_upload_or_readback_preserves_old_copies(synthetic, mode):
    root, remote, env = synthetic
    local_before, remote_before = _inventory(root / "backups"), _inventory(remote)
    result = _run(env, "pre", "2026-10-16_12-02-00", FAKE_RCLONE_MODE=mode)
    assert result.returncode != 0
    assert all(
        _inventory(root / "backups")[name] == digest for name, digest in local_before.items()
    )
    assert all(_inventory(remote)[name] == digest for name, digest in remote_before.items())


def test_insufficient_remote_space_fails_without_upload_or_prune(synthetic):
    root, remote, env = synthetic
    local_before, remote_before = _inventory(root / "backups"), _inventory(remote)
    result = _run(env, "pre", "2026-10-16_12-03-00", FAKE_REMOTE_EXTRA_BYTES="800000000")
    assert result.returncode != 0
    assert _inventory(remote) == remote_before
    assert all(
        _inventory(root / "backups")[name] == digest for name, digest in local_before.items()
    )


def test_nightly_wrapper_lock_blocks_concurrent_release_backup(synthetic):
    root, remote, env = synthetic
    lock = Path(env["DENSTOCK_RELEASE_TEST_LOCK"])
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(env, "pre", "2026-10-16_12-04-00")
    assert result.returncode == 75
    assert len(list((root / "backups").iterdir())) == 31
    assert len(list(remote.iterdir())) == 1


def test_insufficient_local_space_stops_before_creation(synthetic):
    root, remote, env = synthetic
    result = _run(env, "pre", "2026-10-16_12-05-00", DENSTOCK_RELEASE_SYNTHETIC_FREE_BYTES="1")
    assert result.returncode != 0
    assert len(list((root / "backups").iterdir())) == 31
    assert len(list(remote.iterdir())) == 1


def test_unfinished_multipart_upload_stops_before_creation(synthetic):
    root, remote, env = synthetic
    result = _run(env, "pre", "2026-10-16_12-06-00", FAKE_RCLONE_MPU="1")
    assert result.returncode != 0
    assert len(list((root / "backups").iterdir())) == 31
    assert len(list(remote.iterdir())) == 1


def test_invalid_signature_stops_before_remote_upload(synthetic):
    root, remote, env = synthetic
    result = _run(env, "pre", "2026-10-16_12-07-00", FAKE_BAD_SIGNATURE="1")
    assert result.returncode != 0
    assert len(list((root / "backups").iterdir())) == 32
    assert len(list(remote.iterdir())) == 1


def test_failed_remote_listing_stops_before_upload(synthetic):
    _root, remote, env = synthetic
    remote_before = _inventory(remote)
    result = _run(env, "pre", "2026-10-16_12-08-00", FAKE_RCLONE_MODE="listing_error")
    assert result.returncode != 0
    assert _inventory(remote) == remote_before


def test_real_backup_all_without_keep_last_preserves_old_runs(tmp_path, db, settings, monkeypatch):
    """Exercise the actual candidate backup implementation, not the Docker fake."""
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    source_db = tmp_path / "source.sqlite3"
    with sqlite3.connect(source_db) as connection:
        connection.execute("CREATE TABLE example (id INTEGER)")
        connection.execute("INSERT INTO example VALUES (1)")
    media, private, root = (tmp_path / name for name in ("media", "private", "backups"))
    for folder in (media, private, root):
        folder.mkdir()
    (media / "public.bin").write_bytes(b"public-synthetic")
    (private / "attachment.bin").write_bytes(b"private-synthetic")
    for idx in range(31):
        folder = root / f"2026-09-{idx + 1:02d}_03-00-00"
        folder.mkdir()
        (folder / "seed.bin").write_bytes(f"seed-{idx}".encode())
        if idx < 22:
            (folder / "manifest.json").write_text('{"verification_status":"verified"}')
    old = _inventory(root)
    names = iter(("2026-10-16_12-00-00", "2026-10-16_12-01-00"))
    monkeypatch.setattr(backup, "timestamp", lambda: next(names))
    for _ in range(2):
        run = backup.backup_all(
            root=root,
            settings_dict={"ENGINE": "django.db.backends.sqlite3", "NAME": str(source_db)},
            media_root=media, private_media_root=private, keep_last=None,
        )
        assert (run / "db.sqlite3").is_file()
        assert (run / "media.tar.gz").is_file()
        assert (run / "private_media.tar.gz").is_file()
    after = _inventory(root)
    assert all(after[name] == digest for name, digest in old.items())
    assert len([p for p in root.iterdir() if p.is_dir()]) == 33
