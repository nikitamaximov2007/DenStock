import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai_support.runtime import delete_request_directory, stale_request_directories


def age(path: Path, *, hours: int):
    timestamp = time.time() - hours * 3600
    os.utime(path, (timestamp, timestamp))


def test_runtime_purge_is_dry_run_then_deletes_only_old_request_directories(
    settings, tmp_path, capsys
):
    workspace = tmp_path / "runtime"
    workspace.mkdir()
    old_request = workspace / "request-old"
    fresh_request = workspace / "request-fresh"
    unrelated = workspace / "keep-me"
    for path in (old_request, fresh_request, unrelated):
        path.mkdir()
        (path / "data").write_text("fixture", encoding="utf-8")
    age(old_request, hours=48)
    age(unrelated, hours=48)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    settings.AI_SUPPORT_CODEX_RUNTIME_RETENTION_HOURS = 24

    call_command("purge_ai_support_runtime")
    output = capsys.readouterr().out
    assert "directories=1" in output
    assert "DRY RUN" in output
    assert old_request.exists()

    if sys.platform == "linux":
        call_command("purge_ai_support_runtime", "--confirm")
        assert not old_request.exists()
    else:
        with pytest.raises(CommandError, match="changed during cleanup"):
            call_command("purge_ai_support_runtime", "--confirm")
        assert old_request.exists()
    assert fresh_request.exists()
    assert unrelated.exists()


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation is privilege-dependent on Windows")
def test_runtime_purge_never_follows_symlinks(settings, tmp_path, capsys):
    workspace = tmp_path / "runtime"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("keep", encoding="utf-8")
    linked = workspace / "request-linked"
    linked.symlink_to(outside, target_is_directory=True)
    age(outside, hours=48)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    settings.AI_SUPPORT_CODEX_RUNTIME_RETENTION_HOURS = 24

    call_command("purge_ai_support_runtime", "--confirm")
    assert "directories=0" in capsys.readouterr().out
    assert linked.is_symlink()
    assert marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation is privilege-dependent on Windows")
def test_runtime_purge_rejects_symlinked_parent(settings, tmp_path):
    real_parent = tmp_path / "real-parent"
    workspace = real_parent / "runtime"
    workspace.mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(linked_parent / "runtime")

    with pytest.raises(CommandError):
        call_command("purge_ai_support_runtime")


def test_runtime_purge_rejects_unsafe_workspace(settings, tmp_path):
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(tmp_path / "missing")
    with pytest.raises(CommandError):
        call_command("purge_ai_support_runtime")


def test_runtime_purge_rejects_non_positive_retention(settings, tmp_path):
    workspace = tmp_path / "runtime"
    workspace.mkdir()
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    with pytest.raises(CommandError):
        call_command("purge_ai_support_runtime", "--older-than-hours", "0")


def test_runtime_purge_rejects_similar_prefix_and_workspace_root(settings, tmp_path):
    workspace = tmp_path / "runtime"
    workspace.mkdir()
    for name in ("request-", "requestish-old", "request.old"):
        path = workspace / name
        path.mkdir()
        age(path, hours=48)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    assert stale_request_directories(workspace, older_than_hours=24) == []
    with pytest.raises(ValueError, match="unsafe_candidate"):
        delete_request_directory(workspace, workspace)


@pytest.mark.skipif(sys.platform != "linux", reason="Confirmed deletion is Linux-only")
def test_runtime_purge_refuses_when_symlink_safe_rmtree_is_unavailable(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "runtime"
    candidate = workspace / "request-old"
    candidate.mkdir(parents=True)
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    with pytest.raises(RuntimeError, match="symlink_safe_rmtree_unavailable"):
        delete_request_directory(workspace, candidate)
    assert candidate.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Confirmed deletion is Linux-only")
def test_runtime_purge_rejects_directory_replaced_by_symlink(tmp_path):
    workspace = tmp_path / "runtime"
    candidate = workspace / "request-old"
    moved = workspace / "request-moved"
    neighbor = workspace / "keep-me"
    candidate.mkdir(parents=True)
    neighbor.mkdir()
    marker = neighbor / "marker"
    marker.write_text("keep", encoding="utf-8")
    age(candidate, hours=48)
    [planned] = stale_request_directories(workspace, older_than_hours=24)
    candidate.rename(moved)
    candidate.symlink_to(neighbor, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe_candidate"):
        delete_request_directory(workspace, planned)
    assert marker.exists()
    assert moved.exists()


@pytest.mark.skipif(sys.platform == "linux", reason="Linux supports confirmed deletion")
def test_runtime_purge_confirmed_delete_refuses_unsupported_platform(settings, tmp_path):
    workspace = tmp_path / "runtime"
    candidate = workspace / "request-old"
    candidate.mkdir(parents=True)
    age(candidate, hours=48)
    settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
    with pytest.raises(CommandError, match="changed during cleanup"):
        call_command("purge_ai_support_runtime", "--confirm")
    assert candidate.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_runtime_purge_dry_run_ignores_windows_junction(settings, tmp_path, capsys):
    workspace = tmp_path / "runtime"
    outside = tmp_path / "outside"
    junction = workspace / "request-junction"
    workspace.mkdir()
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("keep", encoding="utf-8")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        age(outside, hours=48)
        settings.AI_SUPPORT_CODEX_WORKSPACE = str(workspace)
        call_command("purge_ai_support_runtime")
        assert "directories=0" in capsys.readouterr().out
        assert marker.exists()
    finally:
        junction.rmdir()
