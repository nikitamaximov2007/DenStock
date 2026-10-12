"""A stand-in for ``rclone`` on PATH: local directories only, never a network.

Supports exactly the two calls the backup acceptance uses:

    rclone copy <src-dir> <dst-dir>
    rclone check <src-dir> <dst-dir>

Failure injection for "the upload was cut off" scenarios:

    FAKE_RCLONE_FAIL_AFTER=N    copy only the first N files (sorted), then exit 1
    FAKE_RCLONE_TRUNCATE=NAME   write only half of NAME, then exit 1

Any remote syntax (``name:path``) is refused, so a test can never reach a
real rclone remote by mistake.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path


def _local(arg: str) -> Path:
    if ":" in arg.split("/", 1)[0]:
        sys.exit(f"fake rclone: remote syntax is not allowed here: {arg}")
    return Path(arg)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy(src: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    fail_after = os.environ.get("FAKE_RCLONE_FAIL_AFTER")
    truncate = os.environ.get("FAKE_RCLONE_TRUNCATE")
    for index, item in enumerate(sorted(p for p in src.iterdir() if p.is_file())):
        if fail_after is not None and index >= int(fail_after):
            print("fake rclone: connection reset", file=sys.stderr)
            return 1
        if item.name == truncate:
            data = item.read_bytes()
            (dst / item.name).write_bytes(data[: len(data) // 2])
            print("fake rclone: upload interrupted", file=sys.stderr)
            return 1
        shutil.copy2(item, dst / item.name)
    return 0


def check(src: Path, dst: Path) -> int:
    differences = 0
    for item in sorted(p for p in src.iterdir() if p.is_file()):
        other = dst / item.name
        if not other.is_file() or _digest(other) != _digest(item):
            differences += 1
    print(f"{differences} differences found")
    return 1 if differences else 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in {"copy", "check"}:
        sys.exit("fake rclone: usage: copy|check <src> <dst>")
    command, src, dst = argv[0], _local(argv[1]), _local(argv[2])
    return copy(src, dst) if command == "copy" else check(src, dst)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
