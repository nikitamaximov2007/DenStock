"""Backup-only release candidate: the scenarios the earlier rounds left open.

Everything here is synthetic.  The offsite leg uses ``tests/fake_rclone.py`` on
PATH, which refuses remote syntax, so no test can reach a real rclone remote.
"""

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.db import connection

from apps.customer_requests.storage import PrivateAttachmentStorage
from apps.operations import backup, private_media, restore
from apps.operations.manifest_signing import canonical_manifest_payload
from apps.operations.private_media import archive_inventory, inspect_tree, restore_archive
from tests.emergency_support import sign_production_manifest

pytest_plugins = ("tests.test_private_media_backup",)

sqlite_only = pytest.mark.skipif(
    connection.vendor != "sqlite",
    reason="fixture backups are SQLite; the engine guard rejects them on PostgreSQL",
)
as_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")

# The live /usr/local/sbin/denstock-backup-capped wrapper and the emergency
# station only recognise this shape of run directory name.
PLAIN_RUN_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
FAKE_RCLONE = Path(__file__).resolve().parent / "fake_rclone.py"


def _manifest(run):
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(run, manifest):
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _verified(root: Path) -> list[str]:
    return sorted(
        p.name for p in root.iterdir()
        if (p / "manifest.json").is_file()
        and _manifest(p).get("verification_status") == "verified"
    )


def _tick(monkeypatch):
    """Distinct plain run names without sleeping between backups."""
    names = iter(f"2026-10-12_03-00-{second:02d}" for second in range(60))
    monkeypatch.setattr(backup, "timestamp", lambda: next(names))


# --- Existing-wrapper compatibility ---------------------------------------------


def test_run_directory_name_stays_plain_for_the_existing_wrapper(production):
    run = production["make"]()
    assert PLAIN_RUN_NAME.fullmatch(run.name)
    assert sorted(p.name for p in run.iterdir()) == sorted(
        ["db.sqlite3", "manifest.json", "media.tar.gz", "private_media.tar.gz"]
    )


@sqlite_only
def test_backup_without_keep_last_preserves_existing_generations(production, monkeypatch):
    """Release PRE/POST use the default and must not invoke local retention."""
    _tick(monkeypatch)
    previous = production["make"]()
    previous_manifest = (previous / "manifest.json").read_bytes()

    created = production["make"]()

    assert created != previous
    assert previous.is_dir()
    assert (previous / "manifest.json").read_bytes() == previous_manifest
    assert created.is_dir()


@as_root
def test_unprunable_old_run_does_not_make_backup_all_fail(production, monkeypatch):
    """Production has root-owned run dirs that UID 2001 cannot delete.

    The wrapper runs ``set -euo pipefail``: if pruning raised, the daily
    offsite upload would stop.  Retention must stay best-effort.
    """
    _tick(monkeypatch)
    stuck = production["make"]()
    stuck.chmod(stat.S_IRUSR | stat.S_IXUSR)  # its files can no longer be unlinked
    try:
        production["make"](keep_last=1)
        newest = production["make"](keep_last=1)
    finally:
        stuck.chmod(0o755)
    assert newest.is_dir()
    assert stuck.is_dir()  # left in place, silently, exactly as before this release


def test_failed_backups_never_evict_the_last_verified_generation(production, monkeypatch):
    _tick(monkeypatch)
    good = production["make"](keep_last=1)
    leftover = production["private"] / f"{private_media.RESERVED_PREFIX}x-old"
    leftover.mkdir()
    for _ in range(3):
        with pytest.raises(backup.OperationsError):
            production["make"](keep_last=1)
    assert _verified(production["root"]) == [good.name]
    leftover.rmdir()
    newest = production["make"](keep_last=1)
    assert _verified(production["root"]) == [newest.name]


def test_interrupted_archive_never_produces_a_verified_run(production, monkeypatch):
    """A killed process (no exception handler runs) leaves only an unsigned partial."""
    _tick(monkeypatch)
    real = private_media._refuse_unsafe_members

    def killed(info):
        if info.name.endswith("price.bin"):
            raise KeyboardInterrupt  # stands in for SIGTERM/SIGKILL mid-archive
        return real(info)

    monkeypatch.setattr(private_media, "_refuse_unsafe_members", killed)
    with pytest.raises(KeyboardInterrupt):
        production["make"]()
    monkeypatch.setattr(private_media, "_refuse_unsafe_members", real)
    (partial_run,) = list(production["root"].iterdir())
    assert not (partial_run / "manifest.json").exists()
    assert not (partial_run / "private_media.tar.gz").exists()
    assert not restore.verify_backup(partial_run.name).ok
    newest = production["make"](keep_last=1)
    assert _verified(production["root"]) == [newest.name]
    assert not (newest / "private_media.tar.gz.partial").exists()


# --- Concurrent writers -------------------------------------------------------------


@pytest.mark.parametrize("size", ["shorter", "same", "longer"])
def test_file_changed_while_archiving_triggers_a_fresh_snapshot(production, monkeypatch, size):
    """Shorter content used to abort the backup ("unexpected end of data")."""
    victim = production["private"] / "catalog-imports" / "price.bin"
    original = victim.read_bytes()
    replacement = {
        "shorter": b"rewritten by a bot, shorter",
        "same": bytes(reversed(original)),
        "longer": original + b"appended by a bot",
    }[size]
    real = private_media._refuse_unsafe_members
    state = {"done": False}

    def bot_rewrites_once(info):
        if not state["done"] and info.name.endswith("price.bin"):
            state["done"] = True
            victim.write_bytes(replacement)
        return real(info)

    monkeypatch.setattr(private_media, "_refuse_unsafe_members", bot_rewrites_once)
    run = production["make"]()
    assert archive_inventory(run / "private_media.tar.gz") == inspect_tree(
        production["private"], require_present=True
    )
    target = production["tmp"] / "restored"
    restore_archive(run / "private_media.tar.gz", target, expected=private_media.Inventory(
        _manifest(run)["private_media_file_count"], _manifest(run)["private_media_bytes"],
        _manifest(run)["private_media_tree_sha256"],
    ))
    assert (target / "catalog-imports" / "price.bin").read_bytes() == replacement


# --- Authenticity -------------------------------------------------------------------


@sqlite_only
def test_signed_historical_production_backup_still_verifies_with_a_warning(
    production, settings
):
    """Real production copies made before this release are signed and have no
    private_media fields: they must stay restorable, with an explicit warning."""
    run = production["make"]()
    manifest = _manifest(run)
    for key in [k for k in manifest if k.startswith("private_media")]:
        manifest.pop(key)
    manifest["sha256"].pop("private_media.tar.gz")
    manifest.pop("signature")
    (run / "private_media.tar.gz").unlink()
    sign_production_manifest(manifest, settings)
    _write_manifest(run, manifest)

    report = restore.verify_backup(run.name)

    assert report.ok, report.errors
    assert report.private_media_file == ""
    assert any("старая копия без private_media" in w for w in report.warnings)


@sqlite_only
def test_manifest_signed_by_a_foreign_key_is_rejected(production):
    run = production["make"]()
    manifest = _manifest(run)
    forged = Ed25519PrivateKey.generate().sign(canonical_manifest_payload(manifest))
    manifest["signature"]["value"] = base64.b64encode(forged).decode("ascii")
    _write_manifest(run, manifest)

    report = restore.verify_backup(run.name)

    assert not report.ok
    assert any("подпись manifest недействительна" in e for e in report.errors)


@sqlite_only
def test_wrong_archive_sha256_in_signed_manifest_is_rejected(production):
    run = production["make"]()
    with (run / "private_media.tar.gz").open("ab") as handle:
        handle.write(b"\0")  # still a readable gzip member list, different bytes
    report = restore.verify_backup(run.name)
    assert not report.ok
    assert any("private_media" in e or "контрольн" in e for e in report.errors)


# --- Offsite: upload, cut-off upload, read-back ---------------------------------------


def _rclone(tmp_path, *args, env=None):
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "rclone"
    if not shim.exists():
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_RCLONE}" "$@"\n')
        shim.chmod(0o755)
    environment = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    environment.update(env or {})
    return subprocess.run(["rclone", *args], env=environment, capture_output=True, text=True)


def _read_back(tmp_path, settings, remote_run, name):
    readback_root = tmp_path / f"readback-{name}"
    readback_root.mkdir()
    result = _rclone(tmp_path, "copy", str(remote_run), str(readback_root / remote_run.name))
    assert result.returncode == 0, result.stderr
    settings.BACKUP_ROOT = readback_root
    return restore.verify_backup(remote_run.name)


def test_fake_rclone_refuses_real_remote_syntax(tmp_path):
    result = _rclone(tmp_path, "copy", str(tmp_path), "yandex-s3:bucket/run")
    assert result.returncode != 0
    assert "remote syntax" in result.stderr


@sqlite_only
@pytest.mark.parametrize("cut", ["missing-archive", "truncated-archive"])
def test_incomplete_offsite_upload_is_caught_by_read_back(production, settings, cut):
    run = production["make"]()
    remote = production["tmp"] / "remote-bucket"
    env = (
        {"FAKE_RCLONE_FAIL_AFTER": "3"} if cut == "missing-archive"
        else {"FAKE_RCLONE_TRUNCATE": "private_media.tar.gz"}
    )
    upload = _rclone(production["tmp"], "copy", str(run), str(remote / run.name), env=env)
    assert upload.returncode != 0  # the wrapper's set -e would stop here
    assert (remote / run.name / "manifest.json").is_file()  # manifest got there first

    report = _read_back(production["tmp"], settings, remote / run.name, cut)

    assert not report.ok
    assert any("private_media" in e for e in report.errors)
    check = _rclone(production["tmp"], "check", str(run), str(remote / run.name))
    assert check.returncode != 0 and "0 differences" not in check.stdout


@sqlite_only
def test_complete_offsite_upload_reads_back_verifies_and_restores(production, settings):
    run = production["make"]()
    remote = production["tmp"] / "remote-bucket"
    assert _rclone(production["tmp"], "copy", str(run), str(remote / run.name)).returncode == 0
    check = _rclone(production["tmp"], "check", str(run), str(remote / run.name))
    assert check.returncode == 0 and "0 differences" in check.stdout

    report = _read_back(production["tmp"], settings, remote / run.name, "ok")

    assert report.ok, report.errors
    assert report.private_media_file == "private_media.tar.gz"
    readback_run = Path(settings.BACKUP_ROOT) / run.name
    target = production["tmp"] / "new-private-dir"
    restore_archive(readback_run / "private_media.tar.gz", target,
                    expected=restore._manifest_private_inventory(report.manifest))
    assert _tree(target) == _tree(production["private"])


# --- Legacy attachments still in the public tree --------------------------------------


@sqlite_only
def test_legacy_attachment_in_public_media_is_restored_through_media_archive(
    production, settings
):
    """Two production rows still point at files in PUBLIC media (storage fallback).

    Those bytes travel in media.tar.gz, not in private_media.tar.gz; after a
    restore into new directories the private storage still opens them.
    """
    legacy = production["media"] / "customer_requests" / "legacy-scan.pdf"
    legacy.parent.mkdir()
    legacy.write_bytes(b"%PDF synthetic legacy attachment")
    run = production["make"]()
    assert "customer_requests/legacy-scan.pdf" not in _tree(production["private"])

    new_media = production["tmp"] / "new-media"
    new_private = production["tmp"] / "new-private"
    backup.restore_media(run / "media.tar.gz", media_root=new_media)
    restore_archive(run / "private_media.tar.gz", new_private,
                    expected=restore._manifest_private_inventory(_manifest(run)))
    settings.MEDIA_ROOT = new_media
    settings.PRIVATE_MEDIA_ROOT = new_private
    with PrivateAttachmentStorage().open("customer_requests/legacy-scan.pdf") as handle:
        assert handle.read() == b"%PDF synthetic legacy attachment"


# --- Archive layout ------------------------------------------------------------------


def test_private_archive_is_rooted_at_dot_and_needs_no_re_rooting(production):
    """The 2026-10-11 manual capture was rooted at ``_data/``; ours is not."""
    run = production["make"]()
    with tarfile.open(run / "private_media.tar.gz", "r:gz") as tar:
        names = tar.getnames()
    assert names[0] == "."
    assert all(n == "." or n.startswith("./") for n in names)
    assert not any("_data" in Path(n).parts for n in names)
