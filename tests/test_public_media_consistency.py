"""Public media archive: same before/after/packed consistency as private_media.

Before this change ``media_tree_sha256`` was computed before archiving and the
archive itself was never compared with it, so a photo rewritten while ``tar``
ran produced an archive that silently disagreed with its signed manifest.
Everything here is synthetic.
"""

import os
import tarfile

import pytest

from apps.operations import backup, private_media
from apps.operations.emergency_state import media_tree_sha256
from apps.operations.private_media import archive_inventory, inspect_tree

pytest_plugins = ("tests.test_private_media_backup",)

as_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def _manifest(run):
    import json

    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))


def _mutate_once(monkeypatch, name, action):
    """Run ``action`` the first time tar visits ``name`` (attempt 1 only)."""
    real = private_media._refuse_unsafe_members
    state = {"done": False}

    def hook(info):
        if not state["done"] and info.name.endswith(name):
            state["done"] = True
            action()
        return real(info)

    monkeypatch.setattr(private_media, "_refuse_unsafe_members", hook)
    return state


def test_manifest_hash_is_the_hash_of_the_archive_and_of_the_tree(production):
    run = production["make"]()
    packed = archive_inventory(run / "media.tar.gz")
    assert _manifest(run)["media_tree_sha256"] == packed.tree_sha256
    assert packed.tree_sha256 == media_tree_sha256(production["media"])


@pytest.mark.parametrize("change", ["added", "deleted", "shorter", "longer", "same_size"])
def test_change_during_archiving_takes_a_fresh_consistent_snapshot(
    production, monkeypatch, change
):
    media = production["media"]
    (media / "second.jpg").write_bytes(b"second photo bytes")
    photo = media / "part.jpg"
    original = photo.read_bytes()
    actions = {
        "added": lambda: (media / "uploaded-now.jpg").write_bytes(b"new upload"),
        "deleted": lambda: (media / "second.jpg").unlink(),
        "shorter": lambda: photo.write_bytes(b"x"),
        "longer": lambda: photo.write_bytes(original + b" and more"),
        "same_size": lambda: photo.write_bytes(bytes(reversed(original))),
    }
    state = _mutate_once(monkeypatch, "part.jpg", actions[change])
    run = production["make"]()
    assert state["done"]
    packed = archive_inventory(run / "media.tar.gz")
    assert packed == inspect_tree(media, require_present=True)
    assert _manifest(run)["media_tree_sha256"] == packed.tree_sha256


def test_media_that_never_stops_changing_fails_closed(production, monkeypatch):
    photo = production["media"] / "part.jpg"
    real = private_media._refuse_unsafe_members
    counter = {"n": 0}

    def busy_uploader(info):
        if info.name.endswith("part.jpg"):
            counter["n"] += 1
            photo.write_bytes(b"rewrite %d" % counter["n"])
        return real(info)

    monkeypatch.setattr(private_media, "_refuse_unsafe_members", busy_uploader)
    with pytest.raises(backup.OperationsError, match="Публичные media"):
        production["make"]()
    assert not list(production["root"].glob("*/media.tar.gz"))
    assert not list(production["root"].glob("*/media.tar.gz.partial"))


def test_symlink_in_public_media_is_refused(production):
    (production["media"] / "link.jpg").symlink_to(production["media"] / "part.jpg")
    with pytest.raises(backup.OperationsError, match="Публичные media"):
        production["make"]()


def test_symlink_pointing_outside_public_media_is_refused(production, tmp_path):
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("not public")
    (production["media"] / "escape").symlink_to(secret)
    with pytest.raises(backup.OperationsError, match="Публичные media"):
        production["make"]()


@as_root
def test_unreadable_public_file_fails_closed(production):
    photo = production["media"] / "part.jpg"
    photo.chmod(0)
    try:
        with pytest.raises(backup.OperationsError, match="Публичные media"):
            production["make"]()
    finally:
        photo.chmod(0o644)


def test_interrupted_public_archive_leaves_no_partial_or_verified_run(production, monkeypatch):
    real_open = tarfile.open

    def broken_open(name=None, mode="r", *args, **kwargs):
        if str(name).endswith("media.tar.gz.partial") and mode.startswith("w"):
            raise OSError(28, "No space left on device")
        return real_open(name, mode, *args, **kwargs)

    monkeypatch.setattr(private_media.tarfile, "open", broken_open)
    with pytest.raises(backup.OperationsError):
        production["make"]()
    assert not list(production["root"].glob("*/media.tar.gz*"))
    assert not list(production["root"].glob("*/manifest.json"))


def test_empty_public_media_keeps_the_historical_contract(production):
    for path in production["media"].iterdir():
        path.unlink()
    run = production["make"]()
    assert not (run / "media.tar.gz").exists()
    assert _manifest(run)["media_filename"] is None
    assert _manifest(run)["media_tree_sha256"] == media_tree_sha256(production["media"])
