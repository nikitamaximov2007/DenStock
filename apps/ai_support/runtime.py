import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

REQUEST_DIRECTORY_PREFIX = "request-"
REQUEST_DIRECTORY_PATTERN = re.compile(r"request-[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def _contains_link(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink() or current.is_junction():
            return True
        if current == current.parent:
            return False
        current = current.parent


def validated_local_directory(path) -> Path:
    directory = Path(path)
    if not directory.is_absolute() or _contains_link(directory):
        raise ValueError("unsafe_workspace")
    resolved = directory.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("unsafe_workspace")
    return resolved


def validated_workspace(path) -> Path:
    return validated_local_directory(path)


def stale_request_directories(workspace, *, older_than_hours: int, now=None) -> list[Path]:
    if type(older_than_hours) is not int or older_than_hours <= 0:
        raise ValueError("invalid_retention")
    root = validated_workspace(workspace)
    cutoff = (time.time() if now is None else now) - older_than_hours * 3600
    candidates = []
    for entry in root.iterdir():
        try:
            metadata = entry.lstat()
        except OSError:
            continue
        if (
            not REQUEST_DIRECTORY_PATTERN.fullmatch(entry.name)
            or stat.S_ISLNK(metadata.st_mode)
            or entry.is_junction()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mtime >= cutoff
        ):
            continue
        candidates.append(entry)
    return sorted(candidates)


def delete_request_directory(workspace, candidate: Path) -> None:
    root = validated_workspace(workspace)
    candidate = Path(candidate)
    if candidate.parent != root or not REQUEST_DIRECTORY_PATTERN.fullmatch(candidate.name):
        raise ValueError("unsafe_candidate")
    if sys.platform != "linux":
        raise RuntimeError("confirmed_runtime_purge_requires_linux")
    if shutil.rmtree.avoids_symlink_attacks is not True:
        raise RuntimeError("symlink_safe_rmtree_unavailable")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("safe_directory_open_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    try:
        metadata = os.stat(candidate.name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("unsafe_candidate")
        shutil.rmtree(candidate.name, dir_fd=root_fd)
    finally:
        os.close(root_fd)


def secure_directory_mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)
