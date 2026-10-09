"""Round 2 regression tests for recovery evidence and fail-closed behavior."""

import json
import os
import shutil
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from apps.operations import backup, dr_status
from apps.operations.backup_budget import BudgetError
from apps.operations.dr_local import LocalEncryptedStore
from apps.operations.dr_remote import RcloneDestination
from scripts.operations import dr_mac_pull, dr_upload
from tests.test_dr_local import _keys, _receipt


@pytest.mark.parametrize("target", ["/tmp/outside", "../outside", "missing-target"])
def test_private_media_with_symlink_is_rejected_before_signing(tmp_path, target):
    source = tmp_path / "private"
    source.mkdir()
    (source / "attachment").symlink_to(target)
    with pytest.raises(backup.OperationsError, match="небезопасный элемент"):
        backup.backup_private_media(tmp_path / "run", private_media_root=source)
    assert not (tmp_path / "run" / "private_media.tar.gz").exists()


def test_verified_archive_cannot_contain_symlink_or_escape(tmp_path):
    archive = tmp_path / "private_media.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.addfile(tarfile.TarInfo("."))
        link = tarfile.TarInfo("./attachment")
        link.type = tarfile.SYMTYPE
        link.linkname = "/outside"
        output.addfile(link)
    with pytest.raises(backup.OperationsError):
        backup.verify_media_payload(archive)
    with pytest.raises(backup.OperationsError):
        backup.restore_media(archive, media_root=tmp_path / "restore")
    assert not (tmp_path / "restore").exists()


def test_archive_path_escape_is_rejected(tmp_path):
    archive = tmp_path / "media.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        item = tarfile.TarInfo("../outside")
        item.size = 1
        output.addfile(item, BytesIO(b"x"))
    with pytest.raises(backup.OperationsError):
        backup.verify_media_payload(archive)


def test_run_directories_are_unique_under_concurrency(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "timestamp", lambda: "2026-10-08_03-00-00")
    with ThreadPoolExecutor(max_workers=8) as executor:
        runs = list(executor.map(lambda _: backup.new_run_dir(tmp_path), range(32)))
    assert len(set(runs)) == 32
    assert all(run.is_dir() for run in runs)


def test_no_s3_bucket_wide_cleanup_or_listing_based_remote_delete(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[0] == "lsf":
            return b"generation1/\n"
        if args[0] == "lsjson":
            return json.dumps([{
                "Path": "bundle.tar.age", "Size": 20,
                "ModTime": "2020-01-01T00:00:00Z",
            }]).encode()
        return b"{}"

    destination = RcloneDestination(
        "s3", "fake:shared-bucket/denstock-only", tmp_path / "public.pem", runner=runner,
    )
    destination.cleanup_multipart()
    assert destination.discard_incomplete() == []
    assert not any(call[0] in {"backend", "deletefile", "rmdir"} for call in calls)


def test_duplicate_drive_objects_are_not_verified(tmp_path):
    key, public = _keys(tmp_path)
    name = "generation1"
    receipt = _receipt(key, name, b"cipher")

    def runner(args, **kwargs):
        if args[0] == "lsjson":
            return json.dumps([
                {"Path": "bundle.tar.age", "Size": 6, "ID": "good"},
                {"Path": "bundle.tar.age", "Size": 6, "ID": "other"},
                {"Path": "receipt.json", "Size": 200, "ID": "receipt"},
            ]).encode()
        if args[0] == "cat":
            return json.dumps(receipt).encode()
        raise AssertionError(args)

    destination = RcloneDestination("drive", "fake:DenisStock", public, runner=runner)
    assert not destination.is_verified(name)


def test_cloud_listing_error_is_not_treated_as_missing_generation(tmp_path):
    destination = RcloneDestination(
        "s3", "fake:bucket/dr", tmp_path / "public.pem",
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(BudgetError("API error")),
    )
    with pytest.raises(BudgetError, match="API error"):
        destination.is_verified("generation1")


def test_old_signed_generation_reupload_remains_stale(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    name = "2020-01-01_03-00-00"
    generation = tmp_path / "staging" / name
    generation.mkdir(parents=True)
    body = b"synthetic ciphertext"
    (generation / "bundle.tar.age").write_bytes(body)
    (generation / "receipt.json").write_text(json.dumps(_receipt(
        key, name, body, created_at="2020-01-01T03:00:00+00:00",
    )))
    monkeypatch.setattr(dr_upload, "publish_generation", lambda *args, **kwargs: {
        "bytes": len(body), "removed": [], "reused": True,
    })
    status = tmp_path / "status.json"
    assert dr_upload.run(
        generation.parent, ["google=drive=fake:DenisStock"], public, status,
        allow_best_effort_budget=True,
    ) == 0
    assert dr_status.problems(status, ["google"], timedelta(hours=36))
    entry = dr_status.load(status)["destinations"]["google"]
    assert entry["backup_created_at"].startswith("2020-")
    assert entry["uploaded_at"].startswith(str(datetime.now(UTC).year))


def test_cloud_upload_is_disabled_without_owner_budget_decision(tmp_path):
    with pytest.raises(BudgetError, match="Жёсткий лимит"):
        dr_upload.run(
            tmp_path, ["google=drive=fake:DenisStock"],
            tmp_path / "public.pem", tmp_path / "status.json",
        )


def test_best_effort_upload_still_rejects_forged_receipt(tmp_path, monkeypatch):
    _, public = _keys(tmp_path)
    staged = tmp_path / "staging" / "generation1"
    staged.mkdir(parents=True)
    (staged / "bundle.tar.age").write_bytes(b"synthetic")
    (staged / "receipt.json").write_text(json.dumps({
        "version": 1, "run": "generation1", "bytes": 9,
        "backup_created_at": "2026-10-08T03:00:00+00:00",
    }))
    monkeypatch.setattr(
        dr_upload, "publish_generation",
        lambda *args, **kwargs: pytest.fail("forged receipt reached cloud publisher"),
    )
    with pytest.raises(BudgetError, match="Подпись"):
        dr_upload.run(
            staged.parent, ["google=drive=fake:DenisStock"], public,
            tmp_path / "status.json", allow_best_effort_budget=True,
        )


def test_mac_falls_back_within_same_remote_and_preserves_local(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    valid = b"older synthetic ciphertext"
    receipts = {
        "generation1": _receipt(key, "generation1", valid),
        "generation2": {"version": 1, "run": "generation2"},
    }

    def fake_rclone(*args):
        if args[0] == "lsf":
            return b"generation1/\ngeneration2/\n"
        return json.dumps(receipts[args[1].split("/")[-2]]).encode()

    monkeypatch.setattr(dr_mac_pull, "_rclone", fake_rclone)
    monkeypatch.setattr(
        dr_mac_pull, "_download_bounded",
        lambda source, target, size: Path(target).write_bytes(valid),
    )
    root = tmp_path / "mac"
    assert dr_mac_pull.pull_newest("fake:DenisStock", root, public) == "generation1"
    assert LocalEncryptedStore(root, public).is_verified("generation1")
    receipts["generation1"] = {"version": 1, "run": "generation1"}
    assert dr_mac_pull.pull_newest("fake:DenisStock", root, public) == "generation1"


def test_mac_reports_no_valid_remote_and_keeps_existing_local(tmp_path, monkeypatch):
    key, public = _keys(tmp_path)
    store = LocalEncryptedStore(tmp_path / "mac", public)
    source = tmp_path / "cipher"
    source.write_bytes(b"existing")
    store.install("generation0", source, _receipt(key, "generation0", b"existing"))
    monkeypatch.setattr(dr_mac_pull, "_rclone", lambda *args: (
        b"generation1/\n" if args[0] == "lsf" else b"{}"
    ))
    with pytest.raises(BudgetError, match="Нет доступного"):
        dr_mac_pull.pull_newest("fake:DenisStock", store.root, public)
    assert store.is_verified("generation0")


def test_launchd_template_renders_xml_paths_without_loading_agent(tmp_path):
    if shutil.which("plutil") is None:
        pytest.skip("macOS plutil is unavailable")
    repo = Path(__file__).resolve().parents[1]
    output = tmp_path / "com.denstock.dr-mac-pull.plist"
    config = tmp_path / "read-only & safe.conf"
    env = {
        **os.environ,
        "PYTHON": "/usr/bin/python3",
        "REPO": str(repo),
        "YANDEX_REMOTE": "yandex:denstock",
        "GOOGLE_REMOTE": "google:denstock",
        "BACKUP_DIRECTORY": str(tmp_path / "backups"),
        "PINNED_PUBLIC_KEY": str(tmp_path / "public.pem"),
        "RCLONE_CONFIG": str(config),
        "LOG_DIRECTORY": str(tmp_path / "logs"),
        "OUT": str(output),
    }
    subprocess.run(
        ["bash", str(repo / "scripts/operations/launchd/install-dr-mac-pull.sh")],
        env=env, check=True, capture_output=True, text=True,
    )
    assert "&amp;" in output.read_text()
