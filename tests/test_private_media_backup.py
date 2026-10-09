"""private_media is part of every production backup, and restores all-or-nothing.

Covers the contract cases A-I from the backup-completeness fix plus adversarial
inputs.  All data is synthetic; nothing touches a real private volume.
"""

import hashlib
import io
import json
import os
import sqlite3
import tarfile
import uuid
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from apps.operations import backup, private_media, restore
from apps.operations.emergency_manifest import validate_manifest
from apps.operations.models import RestoreJob
from apps.operations.private_media import (
    RESERVED_PREFIX,
    Inventory,
    PrivateMediaError,
    archive_inventory,
    inspect_tree,
    restore_archive,
)
from tests.emergency_support import configure_test_trust

sqlite_only = pytest.mark.skipif(
    connection.vendor != "sqlite",
    reason="fixture backups are SQLite; the engine guard rejects them on PostgreSQL",
)
as_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def _tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _private_tree(root: Path) -> Path:
    (root / "customer_requests" / "12").mkdir(parents=True)
    (root / "catalog-imports").mkdir()
    (root / "ai-screens" / "empty-dir").mkdir(parents=True)
    (root / "customer_requests" / "12" / "фото заявки.jpg").write_bytes(os.urandom(4096))
    (root / "catalog-imports" / "price.bin").write_bytes(b"\x00" * 10000 + b"tail")
    secret = root / "customer_requests" / "12" / "passport.pdf"
    secret.write_bytes(b"%PDF synthetic")
    secret.chmod(0o600)
    return root


@pytest.fixture
def production(tmp_path, settings, db):
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-production"
    settings.DENSTOCK_APP_COMMIT = "c" * 40
    settings.BACKUP_ROOT = tmp_path / "backups"
    source_db = tmp_path / "source.sqlite3"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('synthetic')")
    media = tmp_path / "media"
    media.mkdir()
    (media / "part.jpg").write_bytes(b"public photo")
    private = _private_tree(tmp_path / "private")
    settings.PRIVATE_MEDIA_ROOT = private

    def make(**overrides):
        kwargs = {
            "settings_dict": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(source_db)},
            "media_root": media,
            "private_media_root": private,
        }
        kwargs.update(overrides)
        return backup.backup_all(**kwargs)

    return {"make": make, "private": private, "media": media, "tmp": tmp_path,
            "settings": settings, "root": tmp_path / "backups"}


def _manifest(run):
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))


def _expected(run) -> Inventory:
    m = _manifest(run)
    return Inventory(m["private_media_file_count"], m["private_media_bytes"],
                     m["private_media_tree_sha256"])


def _verified_runs(root: Path):
    return [p for p in root.iterdir() if (p / "manifest.json").is_file()] if root.exists() else []


# --- A. New backup with private_media -----------------------------------------


def test_a_backup_includes_signed_private_media_and_restores_identically(production):
    run = production["make"]()
    m = _manifest(run)
    assert m["private_media_status"] == "included"
    assert m["private_media_filename"] == "private_media.tar.gz"
    assert m["private_media_file_count"] == 3
    assert m["private_media_bytes"] == sum(
        p.stat().st_size for p in production["private"].rglob("*") if p.is_file()
    )
    assert m["sha256"]["private_media.tar.gz"] == m["private_media_sha256"]
    assert m["signature"]["algorithm"] == "ed25519"
    assert validate_manifest(run, expected_source="production").ok
    target = production["tmp"] / "restored"
    restore_archive(run / "private_media.tar.gz", target, expected=_expected(run))
    assert _tree(target) == _tree(production["private"])
    assert (target / "ai-screens" / "empty-dir").is_dir()
    assert ((target / "customer_requests/12/passport.pdf").stat().st_mode & 0o777) == 0o600
    assert not (run / "private_media.tar.gz.partial").exists()


# --- B. Empty private_media ----------------------------------------------------


def test_b_empty_private_media_is_recorded_and_restores_as_empty(production, tmp_path):
    empty = tmp_path / "empty-private"
    empty.mkdir()
    run = production["make"](private_media_root=empty)
    m = _manifest(run)
    assert m["private_media_status"] == "included"
    assert m["private_media_file_count"] == 0 and m["private_media_bytes"] == 0
    target = tmp_path / "target"
    (target / "stale").mkdir(parents=True)
    (target / "stale" / "old.bin").write_bytes(b"old")
    restore_archive(run / "private_media.tar.gz", target, expected=_expected(run))
    assert list(target.iterdir()) == []  # restore replaces, it does not merge


# --- C. Missing mount ----------------------------------------------------------


def test_c_missing_private_volume_fails_production_backup(production, tmp_path):
    with pytest.raises(backup.OperationsError, match="не найден"):
        production["make"](private_media_root=tmp_path / "not-mounted")
    assert _verified_runs(production["root"]) == []


def test_c_outside_production_absence_is_explicit(production, tmp_path, settings):
    settings.DENSTOCK_MODE = "development"
    run = production["make"](private_media_root=tmp_path / "absent")
    m = _manifest(run)
    assert m["private_media_status"] == "absent"
    assert m["private_media_filename"] is None
    assert not (run / "private_media.tar.gz").exists()
    assert validate_manifest(run).ok


def test_c_production_manifest_claiming_absent_is_invalid(production):
    run = production["make"]()
    m = _manifest(run)
    for key in ("private_media_filename", "private_media_sha256", "private_media_tree_sha256",
                "private_media_file_count", "private_media_bytes"):
        m[key] = None
    m["private_media_status"] = "absent"
    m["sha256"].pop("private_media.tar.gz")
    (run / "manifest.json").write_text(json.dumps(m))
    errors = validate_manifest(run).errors
    assert "production-бэкап без private_media недопустим" in errors


# --- D. Unreadable file / directory ---------------------------------------------


@as_root
def test_d_unreadable_file_fails_the_backup(production):
    victim = production["private"] / "catalog-imports" / "price.bin"
    victim.chmod(0o000)
    try:
        with pytest.raises(backup.OperationsError, match="catalog-imports/price.bin"):
            production["make"]()
    finally:
        victim.chmod(0o644)
    assert _verified_runs(production["root"]) == []


@as_root
def test_d_unreadable_directory_fails_the_backup(production):
    folder = production["private"] / "customer_requests"
    folder.chmod(0o000)
    try:
        with pytest.raises(backup.OperationsError, match="Нет доступа"):
            production["make"]()
    finally:
        folder.chmod(0o755)
    assert _verified_runs(production["root"]) == []


# --- E. Corrupted archive --------------------------------------------------------


@sqlite_only
def test_e_corrupted_archive_is_rejected_everywhere(production, tmp_path):
    run = production["make"]()
    archive = run / "private_media.tar.gz"
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(bytes(data))
    assert "контрольная сумма private_media не совпадает" in validate_manifest(run).errors
    report = restore.verify_backup(run.name)
    assert not report.ok
    with pytest.raises(CommandError):
        call_command("restore_private_media", run.name, "--yes")
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"current")
    with pytest.raises(PrivateMediaError):
        restore_archive(archive, target, expected=_expected(run))
    with pytest.raises(PrivateMediaError, match="повреждён"):
        archive_inventory(archive)  # the gzip CRC is checked even without a manifest
    assert _tree(target) == {"keep.bin": hashlib.sha256(b"current").hexdigest()}


def test_e_truncated_archive_is_rejected(tmp_path):
    archive = tmp_path / "private_media.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("./a.bin")
        info.size = 100000
        tar.addfile(info, io.BytesIO(os.urandom(100000)))
    archive.write_bytes(archive.read_bytes()[:2000])
    with pytest.raises(PrivateMediaError, match="повреждён"):
        archive_inventory(archive)


# --- F. Tampered manifest ---------------------------------------------------------


@sqlite_only
def test_f_tampered_manifest_fails_signature_verification(production):
    run = production["make"]()
    m = _manifest(run)
    m["private_media_file_count"] = 99
    (run / "manifest.json").write_text(json.dumps(m))
    report = restore.verify_backup(run.name)
    assert any("подпись manifest недействительна" in e for e in report.errors)


@sqlite_only
def test_f_swapped_archive_with_rewritten_checksum_is_still_rejected(production, tmp_path):
    run = production["make"]()
    forged_source = tmp_path / "forged"
    forged_source.mkdir()
    (forged_source / "evil.bin").write_bytes(b"attacker data")
    forged, inventory = private_media.create_archive(forged_source, tmp_path)
    (run / "private_media.tar.gz").write_bytes(forged.read_bytes())
    m = _manifest(run)
    digest = hashlib.sha256(forged.read_bytes()).hexdigest()
    m.update(private_media_sha256=digest, private_media_tree_sha256=inventory.tree_sha256,
             private_media_file_count=1, private_media_bytes=inventory.bytes)
    m["sha256"]["private_media.tar.gz"] = digest
    (run / "manifest.json").write_text(json.dumps(m))
    report = restore.verify_backup(run.name)
    assert not report.ok
    assert any("подпись" in e for e in report.errors)


# --- G. Old backup without private_media -----------------------------------------


@sqlite_only
def test_g_historical_backup_stays_valid_and_restorable(production, settings,
                                                       django_user_model, monkeypatch):
    settings.DENSTOCK_MODE = "development"
    run = production["make"]()
    m = _manifest(run)
    for key in list(m):
        if key.startswith("private_media"):
            m.pop(key)
    m["sha256"].pop("private_media.tar.gz")
    (run / "private_media.tar.gz").unlink()
    (run / "manifest.json").write_text(json.dumps(m))
    assert validate_manifest(run).ok
    report = restore.verify_backup(run.name)
    assert report.ok and report.private_media_file == ""
    assert any("старая копия без private_media" in w for w in report.warnings)

    before = _tree(production["private"])
    user = django_user_model.objects.create_superuser("owner", "o@example.com", "x")
    monkeypatch.setattr(restore.backup, "backup_all", lambda **kw: run)
    monkeypatch.setattr(restore.backup, "restore_db", lambda p: [])
    monkeypatch.setattr(restore, "call_command", lambda *a, **kw: None)
    monkeypatch.setattr(restore.connections, "close_all", lambda: None)
    job = restore.run_web_restore(run.name, user=user)
    assert job.status == RestoreJob.Status.COMPLETED, job.error
    assert "оставлены как есть" in job.log
    assert _tree(production["private"]) == before


# --- H. Interrupted restore -------------------------------------------------------


def test_h_interrupted_restore_leftovers_block_backup_and_restore(production, tmp_path):
    run = production["make"]()
    leftover = production["private"] / f"{RESERVED_PREFIX}deadbeef-old"
    leftover.mkdir()
    with pytest.raises(backup.OperationsError, match="прерванного восстановления"):
        production["make"]()
    with pytest.raises(PrivateMediaError, match="прервано"):
        restore_archive(run / "private_media.tar.gz", production["private"],
                        expected=_expected(run))
    assert leftover.is_dir()  # never auto-deleted: it may hold the previous files


# --- I. Rollback after failure -----------------------------------------------------


def test_i_failure_during_swap_puts_the_previous_files_back(production, tmp_path, monkeypatch):
    run = production["make"]()
    target = tmp_path / "live"
    (target / "keep").mkdir(parents=True)
    (target / "keep" / "current.bin").write_bytes(b"current-1")
    (target / "top.bin").write_bytes(b"current-2")
    before = _tree(target)
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 4:  # old contents moved away, new ones half placed
            raise OSError("No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(private_media.os, "replace", flaky)
    with pytest.raises(PrivateMediaError, match="прежние файлы возвращены"):
        restore_archive(run / "private_media.tar.gz", target, expected=_expected(run))
    monkeypatch.setattr(private_media.os, "replace", real_replace)
    assert _tree(target) == before
    assert not [p for p in target.iterdir() if p.name.startswith(RESERVED_PREFIX)]


@sqlite_only
def test_i_web_restore_aborts_before_the_database_when_private_media_fails(
    production, django_user_model, monkeypatch,
):
    run = production["make"]()
    user = django_user_model.objects.create_superuser("owner", "o@example.com", "x")
    database_touched = []
    monkeypatch.setattr(restore.backup, "backup_all", lambda **kw: run)
    monkeypatch.setattr(restore.backup, "restore_db", lambda p: database_touched.append(p))
    monkeypatch.setattr(restore, "call_command", lambda *a, **kw: None)
    monkeypatch.setattr(restore.connections, "close_all", lambda: None)
    monkeypatch.setattr(restore, "restore_archive",
                        lambda *a, **kw: (_ for _ in ()).throw(PrivateMediaError("disk full")))
    job = restore.run_web_restore(run.name, user=user)
    assert job.status == RestoreJob.Status.FAILED
    assert database_touched == []
    assert "База и media не изменялись" in job.log


@sqlite_only
def test_web_restore_replaces_private_media_with_the_backup(production, django_user_model,
                                                          monkeypatch):
    run = production["make"]()
    expected = _tree(production["private"])
    (production["private"] / "created-after-backup.bin").write_bytes(b"newer")
    user = django_user_model.objects.create_superuser("owner", "o@example.com", "x")
    monkeypatch.setattr(restore.backup, "backup_all", lambda **kw: run)
    monkeypatch.setattr(restore.backup, "restore_db", lambda p: [])
    monkeypatch.setattr(restore, "call_command", lambda *a, **kw: None)
    monkeypatch.setattr(restore.connections, "close_all", lambda: None)
    job = restore.run_web_restore(run.name, user=user)
    assert job.status == RestoreJob.Status.COMPLETED, job.error
    assert _tree(production["private"]) == expected


@sqlite_only
def test_management_command_restores_and_requires_confirmation(production):
    run = production["make"]()
    expected = _tree(production["private"])
    (production["private"] / "catalog-imports" / "price.bin").write_bytes(b"damaged")
    with pytest.raises(CommandError, match="--yes"):
        call_command("restore_private_media", run.name)
    call_command("restore_private_media", run.name, "--yes")
    assert _tree(production["private"]) == expected


# --- Adversarial: symlinks, special files, malicious archives ---------------------


@pytest.mark.parametrize("kind", ["absolute", "relative-escape", "dangling", "nested", "dir"])
def test_symlinks_in_private_media_are_refused(production, tmp_path, kind):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"not private media")
    link = production["private"] / ("customer_requests/12/link" if kind == "nested" else "link")
    target = {"absolute": outside, "relative-escape": Path("../outside.bin"),
              "dangling": tmp_path / "missing", "nested": outside, "dir": tmp_path}[kind]
    link.symlink_to(target)
    with pytest.raises(backup.OperationsError, match="ссылка"):
        production["make"]()
    assert _verified_runs(production["root"]) == []


def test_private_root_itself_being_a_symlink_is_refused(production, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(production["private"])
    with pytest.raises(backup.OperationsError, match="ссылкой"):
        production["make"](private_media_root=alias)


def test_special_file_is_refused(production):
    os.mkfifo(production["private"] / "pipe")
    with pytest.raises(backup.OperationsError, match="Специальный файл"):
        production["make"]()


def _evil_archive(tmp_path, build):
    archive = tmp_path / "private_media.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        build(tar)
    return archive


def _file(tar, name, data=b"x"):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize("case", [
    "traversal", "absolute", "symlink", "hardlink", "device", "duplicate", "reserved",
])
def test_malicious_archives_never_touch_the_target(tmp_path, case):
    def build(tar):
        if case == "traversal":
            _file(tar, "../escape.bin")
        elif case == "absolute":
            _file(tar, "/etc/escape.bin")
        elif case in ("symlink", "hardlink"):
            info = tarfile.TarInfo("./link")
            info.type = tarfile.SYMTYPE if case == "symlink" else tarfile.LNKTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        elif case == "device":
            info = tarfile.TarInfo("./dev")
            info.type = tarfile.CHRTYPE
            tar.addfile(info)
        elif case == "duplicate":
            _file(tar, "./a.bin", b"one")
            _file(tar, "./a.bin", b"two")
        elif case == "reserved":
            _file(tar, f"./{RESERVED_PREFIX}x-old/a.bin")

    archive = _evil_archive(tmp_path, build)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"current")
    with pytest.raises(PrivateMediaError):
        restore_archive(archive, target, expected=Inventory(0, 0, "0" * 64))
    with pytest.raises(PrivateMediaError):
        archive_inventory(archive)
    assert sorted(p.name for p in target.iterdir()) == ["keep.bin"]
    assert not (tmp_path / "escape.bin").exists()


# --- Concurrent writers (Telegram/MAX bots share the volume) ----------------------


def test_file_added_while_archiving_triggers_a_fresh_consistent_snapshot(production,
                                                                         monkeypatch):
    real = private_media._refuse_unsafe_members
    state = {"done": False}

    def bot_writes_once(info):
        if not state["done"]:
            state["done"] = True
            (production["private"] / "customer_requests" / "late.jpg").write_bytes(b"late")
        return real(info)

    monkeypatch.setattr(private_media, "_refuse_unsafe_members", bot_writes_once)
    run = production["make"]()
    m = _manifest(run)
    assert m["private_media_file_count"] == 4  # the archive matches the final tree
    assert archive_inventory(run / "private_media.tar.gz") == inspect_tree(
        production["private"], require_present=True,
    )


def test_tree_that_never_settles_fails_instead_of_archiving_a_mix(production, monkeypatch):
    real = private_media._refuse_unsafe_members
    counter = {"n": 0}

    def bot_always_writes(info):
        counter["n"] += 1
        (production["private"] / f"churn-{counter['n']}.bin").write_bytes(b"x")
        return real(info)

    monkeypatch.setattr(private_media, "_refuse_unsafe_members", bot_always_writes)
    with pytest.raises(backup.OperationsError, match="изменялся во время копирования"):
        production["make"]()
    assert _verified_runs(production["root"]) == []
    assert not list(production["root"].glob("*/private_media.tar.gz*"))


# --- Offsite package / UI completeness ---------------------------------------------


def test_backup_directory_is_a_complete_self_describing_package(production):
    run = production["make"]()
    m = _manifest(run)
    files = sorted(p.name for p in run.iterdir())
    assert files == sorted(["manifest.json", *m["sha256"]])
    assert "private_media.tar.gz" in files


@sqlite_only
def test_backups_ui_lists_and_serves_the_private_archive_to_admins_only(
    production, client, django_user_model,
):
    run = production["make"]()
    admin = django_user_model.objects.create_superuser("boss", "b@example.com", "x")
    clerk = django_user_model.objects.create_user("clerk", password="x")
    from django.urls import reverse

    url = reverse("operations:backup_download", args=[run.name, "private_media.tar.gz"])
    client.force_login(clerk)
    assert client.get(url).status_code == 403
    client.force_login(admin)
    response = client.get(url)
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == (run / "private_media.tar.gz").read_bytes()


def test_hard_link_is_refused_explicitly(production):
    original = production["private"] / "catalog-imports" / "price.bin"
    os.link(original, production["private"] / "catalog-imports" / "price-copy.bin")
    with pytest.raises(backup.OperationsError, match="Жёсткая ссылка"):
        production["make"]()
    assert _verified_runs(production["root"]) == []


def test_file_deleted_while_archiving_triggers_a_fresh_snapshot(production, monkeypatch):
    victim = production["private"] / "catalog-imports" / "price.bin"
    real = private_media._refuse_unsafe_members
    state = {"done": False}

    def bot_deletes_once(info):
        if not state["done"] and info.name.endswith("price.bin"):
            state["done"] = True
            victim.unlink()  # removed after tar stat'ed it and before it opens it
        return real(info)

    monkeypatch.setattr(private_media, "_refuse_unsafe_members", bot_deletes_once)
    real_open = private_media.tarfile.open
    archives_started = []

    def counting_open(*args, **kwargs):
        if kwargs.get("mode", args[1] if len(args) > 1 else "") == "w:gz":
            archives_started.append(1)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(private_media.tarfile, "open", counting_open)
    run = production["make"]()
    monkeypatch.setattr(private_media.tarfile, "open", real_open)
    assert len(archives_started) >= 2  # the vanished file forced a second snapshot
    assert _manifest(run)["private_media_file_count"] == 2
    assert "catalog-imports/price.bin" not in _tree(production["private"])


def test_disk_full_while_archiving_leaves_no_partial_and_no_verified_run(production,
                                                                         monkeypatch):
    import types

    def full_disk(*args, **kwargs):
        if kwargs.get("mode", args[1] if len(args) > 1 else "") == "w:gz":
            raise OSError(28, "No space left on device")
        return tarfile.open(*args, **kwargs)

    # Only the private-media module sees the full disk (public media is unchanged).
    fake = types.SimpleNamespace(open=full_disk, TarError=tarfile.TarError,
                                 TarInfo=tarfile.TarInfo)
    monkeypatch.setattr(private_media, "tarfile", fake)
    with pytest.raises(backup.OperationsError, match="No space left"):
        production["make"]()
    assert _verified_runs(production["root"]) == []
    assert not list(production["root"].glob("*/private_media.tar.gz*"))


def test_failure_while_extracting_leaves_the_target_untouched(production, tmp_path,
                                                              monkeypatch):
    run = production["make"]()
    target = tmp_path / "live"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"current")

    def full_disk(self, path, *args, **kwargs):
        (Path(path) / "half.bin").write_bytes(b"half")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tarfile.TarFile, "extractall", full_disk)
    with pytest.raises(PrivateMediaError, match="No space left"):
        restore_archive(run / "private_media.tar.gz", target, expected=_expected(run))
    assert sorted(p.name for p in target.iterdir()) == ["keep.bin"]
