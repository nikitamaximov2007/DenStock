import os
import shutil
import stat
import time
from pathlib import Path

REQUEST_DIRECTORY_PREFIX = "request-"


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
            not entry.name.startswith(REQUEST_DIRECTORY_PREFIX)
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mtime >= cutoff
        ):
            continue
        try:
            resolved = entry.resolve(strict=True)
        except OSError:
            continue
        if root not in resolved.parents:
            continue
        candidates.append(resolved)
    return sorted(candidates)


def delete_request_directory(workspace, candidate: Path) -> None:
    root = validated_workspace(workspace)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("unsafe_candidate") from exc
    if (
        not candidate.name.startswith(REQUEST_DIRECTORY_PREFIX)
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or root not in resolved.parents
    ):
        raise ValueError("unsafe_candidate")
    shutil.rmtree(resolved)


def secure_directory_mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)
