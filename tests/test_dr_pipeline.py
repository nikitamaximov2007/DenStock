import json
import re
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from apps.operations import backup, dr_status
from apps.operations import restore as restore_mod
from apps.operations.backup_budget import BudgetError
from apps.operations.dr_archive import encrypt_verified_run
from apps.operations.models import RestoreJob
from scripts.operations import dr_check, dr_mac_pull, dr_upload
from tests.emergency_support import configure_test_trust
from tests.test_dr_local import _keys, _receipt


def _production_run(tmp_path, settings):
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-production"
    settings.DENSTOCK_APP_COMMIT = "a" * 40
    private = tmp_path / "private"
    private.mkdir()
    (private / "request.bin").write_bytes(b"synthetic private file")
    media = tmp_path / "media"
    media.mkdir()
    (media / "part.jpg").write_bytes(b"synthetic ordinary file")
    database = tmp_path / "sample.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE synthetic (value TEXT)")
    settings.PRIVATE_MEDIA_ROOT = private
    return backup.backup_all(
        root=tmp_path / "backups", media_root=media,
        settings_dict={"ENGINE": "django.db.backends.sqlite3", "NAME": str(database)},
    )


def _recipient(tmp_path):
    out = subprocess.run(
        ["age-keygen", "-o", str(tmp_path / "age.txt")], check=True, capture_output=True,
        text=True,
    )
    return re.search(r"age1[a-z0-9]+", out.stdout + out.stderr).group(0)


@pytest.mark.django_db
def test_dr_encrypt_stages_ciphertext_and_signed_receipt_only(tmp_path, settings):
    run = _production_run(tmp_path, settings)
    settings.BACKUP_ROOT = run.parent
    recipient = _recipient(tmp_path)
    with pytest.raises(CommandError, match="ВНЕ"):
        call_command("dr_encrypt", staging=str(run.parent / "dr-staging"), recipient=[recipient])
    staging = tmp_path / "staging"
    staging.mkdir()
    call_command("dr_encrypt", staging=str(staging), recipient=[recipient])
    generation = staging / run.name
    assert sorted(p.name for p in generation.iterdir()) == ["bundle.tar.age", "receipt.json"]
    assert not list(staging.glob(".work-*"))  # no temporary leftovers
    assert b"synthetic private file" not in (generation / "bundle.tar.age").read_bytes()
    receipt = json.loads((generation / "receipt.json").read_text())
    from apps.operations.dr_local import verify_receipt
    assert verify_receipt(
        receipt, settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH,
        settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID,
    )
    assert receipt["bytes"] == (generation / "bundle.tar.age").stat().st_size


@pytest.mark.django_db
def test_dr_encrypt_requires_public_recipient_and_a_private_media_backup(tmp_path, settings):
    settings.BACKUP_ROOT = tmp_path / "empty"
    settings.BACKUP_ROOT.mkdir()
    with pytest.raises(CommandError, match="recipient"):
        call_command("dr_encrypt", staging=str(tmp_path / "s"))
    with pytest.raises(CommandError, match="private_media"):
        call_command("dr_encrypt", staging=str(tmp_path / "s"), recipient=["age1abc"])


@pytest.mark.django_db
def test_private_key_material_is_not_accepted_as_recipient(tmp_path, settings):
    run = _production_run(tmp_path, settings)
    from apps.operations.dr_archive import ArchiveError
    for bad in ("AGE-SECRET-KEY-1ABC", "age1abc; rm -rf", ""):
        with pytest.raises(ArchiveError):
            encrypt_verified_run(run, tmp_path / "x.age", bad)
    assert not (tmp_path / "x.age").exists()
    assert not (tmp_path / "x.age.partial").exists()


@pytest.mark.django_db
def test_web_restore_restores_private_media_too(
    tmp_path, settings, django_user_model, monkeypatch
):
    if connection.vendor != "sqlite":
        pytest.skip("The fixture backup is SQLite; the engine guard rejects it on PostgreSQL")
    run = _production_run(tmp_path, settings)
    settings.BACKUP_ROOT = run.parent
    settings.DENSTOCK_MODE = "test"
    target_private = tmp_path / "live-private"
    settings.PRIVATE_MEDIA_ROOT = target_private
    settings.MEDIA_ROOT = tmp_path / "live-media"
    user = django_user_model.objects.create_superuser("owner", "o@example.com", "x")
    monkeypatch.setattr(restore_mod.backup, "backup_all", lambda **kw: run)
    monkeypatch.setattr(restore_mod.backup, "restore_db", lambda p: [])
    monkeypatch.setattr(restore_mod, "call_command", lambda *a, **kw: None)
    monkeypatch.setattr(restore_mod.connections, "close_all", lambda: None)
    job = restore_mod.run_web_restore(run.name, user=user)
    assert job.status == RestoreJob.Status.COMPLETED, job.error
    assert (target_private / "request.bin").read_bytes() == b"synthetic private file"


def test_status_flags_missing_stale_and_failed_destinations(tmp_path):
    status = tmp_path / "status.json"
    now = datetime(2026, 10, 8, tzinfo=UTC)
    day = timedelta(hours=36)
    assert len(dr_status.problems(status, ["yandex", "google"], day, now)) == 2
    dr_status.record(status, "yandex", ok=True, run="r", now=now - timedelta(hours=1))
    dr_status.record(status, "google", ok=True, run="r", now=now - timedelta(hours=48))
    assert dr_status.problems(status, ["yandex"], day, now) == []
    assert "старше" in dr_status.problems(status, ["google"], day, now)[0]
    dr_status.record(status, "yandex", ok=False, error="x" * 1000, now=now)
    found = dr_status.problems(status, ["yandex"], day, now)
    assert "ошибкой" in found[0]
    entry = dr_status.load(status)["destinations"]["yandex"]
    assert entry["last_success_at"] and len(entry["error"]) <= 300
    assert (status.stat().st_mode & 0o777) == 0o600


def test_dr_check_exit_codes(tmp_path, monkeypatch, capsys):
    status = tmp_path / "s.json"
    argv = ["dr_check", "--status-file", str(status), "--label", "yandex"]
    monkeypatch.setattr("sys.argv", argv)
    assert dr_check.main() == 2
    dr_status.record(status, "yandex", ok=True, run="r")
    assert dr_check.main() == 0


def _stage(tmp_path, key):
    name = "2026-10-08_03-00-00"
    generation = tmp_path / "staging" / name
    generation.mkdir(parents=True)
    data = b"c" * 10
    (generation / "bundle.tar.age").write_bytes(data)
    (generation / "receipt.json").write_text(json.dumps(_receipt(key, name, data)))
    return tmp_path / "staging", name


def test_upload_isolates_destinations_and_exits_nonzero_on_any_failure(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    staging, name = _stage(tmp_path, key)
    seen = []

    def fake_publish(destination, run, bundle, receipt):
        seen.append(destination.remote)
        if destination.kind == "drive":
            raise BudgetError("Google недоступен")
        return {"run": run, "removed": [], "bytes": 123, "reused": False}

    monkeypatch.setattr(dr_upload, "publish_generation", fake_publish)
    status = tmp_path / "status.json"
    code = dr_upload.run(
        staging, ["yandex=s3=y:bucket/dr", "google=drive=g:DenisStock"], public, status,
    )
    assert code == 1
    assert seen == ["y:bucket/dr", "g:DenisStock"]  # drive failure did not stop yandex
    data = dr_status.load(status)["destinations"]
    assert data["yandex"]["ok"] and not data["google"]["ok"]


def test_upload_refuses_overlapping_or_root_destinations(tmp_path):
    key, public = _keys(tmp_path)
    staging, _ = _stage(tmp_path, key)
    for destinations in (
        ["a=s3=y:bucket/dr", "b=s3=y:bucket/dr/sub"],
        ["a=s3=y:bucket"],
        ["a=drive=g:", ],
        ["a=s3=y:bucket/dr", "a=drive=g:DenisStock"],
    ):
        with pytest.raises(BudgetError):
            dr_upload.run(staging, destinations, public, tmp_path / "s.json")


def test_mac_pull_uses_every_remote_and_reports_partial_failure(tmp_path, monkeypatch, capsys):
    key, public = _keys(tmp_path)
    name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    data = b"cipher-bytes"

    def fake_pull(remote, root, public_key):
        if remote.startswith("g:"):
            raise BudgetError("Google down")
        from apps.operations.dr_local import LocalEncryptedStore
        store = LocalEncryptedStore(root, public_key)
        source = Path(root).parent / "src"
        source.write_bytes(data)
        if not store.is_verified(name):
            store.install(name, source, _receipt(key, name, data))
        return name

    monkeypatch.setattr(dr_mac_pull, "pull_newest", fake_pull)
    monkeypatch.setattr("sys.argv", [
        "x", "--remote", "y:bucket/dr", "--remote", "g:DenisStock",
        "--root", str(tmp_path / "mac"), "--public-key", str(public),
    ])
    assert dr_mac_pull.main() == 3  # one remote failed, a local copy still exists
    assert name in capsys.readouterr().out


def test_mac_pull_flags_stale_local_copy(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    old = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d_%H-%M-%S")
    from apps.operations.dr_local import LocalEncryptedStore
    store = LocalEncryptedStore(tmp_path / "mac", public)
    source = tmp_path / "src"
    source.write_bytes(b"old")
    store.install(old, source, _receipt(key, old, b"old"))
    monkeypatch.setattr(dr_mac_pull, "pull_newest", lambda *a: old)
    monkeypatch.setattr("sys.argv", [
        "x", "--remote", "y:bucket/dr", "--root", str(tmp_path / "mac"),
        "--public-key", str(public),
    ])
    assert dr_mac_pull.main() == 4


def test_launchd_template_has_every_placeholder_and_valid_xml():
    path = Path("scripts/operations/launchd/com.denstock.dr-mac-pull.plist.in")
    text = path.read_text()
    found = set(re.findall(r"__[A-Z_]+__", text))
    assert {"__PYTHON__", "__REPO__", "__RCLONE_CONFIG__", "__LOG_DIRECTORY__"} <= found
    rendered = text
    for placeholder in found:
        rendered = rendered.replace(placeholder, "/x")
    import plistlib
    parsed = plistlib.loads(rendered.encode())
    assert parsed["StartInterval"] == 3600 and parsed["RunAtLoad"] is True
    assert "/opt/homebrew/bin" in parsed["EnvironmentVariables"]["PATH"]


def test_mac_pull_runs_unmodified_against_a_local_rclone_stub(tmp_path, monkeypatch):
    """Exercise the real subprocess/streaming code with a filesystem-backed rclone stand-in."""
    key, public = _keys(tmp_path)
    name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    data = b"c" * 4096
    cloud = tmp_path / "cloud" / "dr" / name
    cloud.mkdir(parents=True)
    (cloud / "bundle.tar.age").write_bytes(data)
    (cloud / "receipt.json").write_text(json.dumps(_receipt(key, name, data)))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "rclone"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = [a for a in sys.argv[1:]]\n"
        f"root = {str(tmp_path / 'cloud')!r}\n"
        "cmd = args[0]\n"
        "target = next(a for a in args[1:] if ':' in a).split(':', 1)[1]\n"
        "path = os.path.join(root, target)\n"
        "if cmd == 'lsf':\n"
        "    print(''.join(n + '/\\n' for n in sorted(os.listdir(path))))\n"
        "elif cmd == 'cat':\n"
        "    sys.stdout.buffer.write(open(path, 'rb').read())\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setattr("sys.argv", [
        "x", "--remote", "fake:dr", "--root", str(tmp_path / "mac"),
        "--public-key", str(public),
    ])
    assert dr_mac_pull.main() == 0
    assert (tmp_path / "mac" / name / "bundle.tar.age").read_bytes() == data
    assert not list((tmp_path / "mac").glob(".incoming-*"))  # no leftover partial downloads
