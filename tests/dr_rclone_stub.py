#!/usr/bin/env python3
"""Filesystem-backed stand-in for the rclone subcommands the DR code uses.

``remote:path`` maps to ``$DR_STUB_ROOT/remote/path``.  It is a test double for
command plumbing only; it says nothing about real provider accounting.
"""

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(os.environ["DR_STUB_ROOT"])


def local(spec: str) -> Path:
    name, _, path = spec.partition(":")
    return ROOT / name / path.strip("/")


def main(argv):
    cmd, rest = argv[0], argv[1:]
    # Remote specs contain ":"; option values such as "--max-depth 1" do not.
    positional = [a for a in rest if ":" in a and not a.startswith("-")]
    if cmd == "copyto":
        positional = [next(a for a in rest if not a.startswith("-") and ":" not in a),
                      *positional]
    if cmd == "size":
        target = local(positional[-1])
        total = sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) \
            if target.exists() else 0
        if "--drive-trashed-only" in rest:
            total = 0
        print(json.dumps({"count": 0, "bytes": total, "sizeless": 0}))
    elif cmd == "about":
        target = local(positional[-1].split("/")[0] + "/")
        used = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
        print(json.dumps({"used": used}))
    elif cmd == "backend" and rest[0] == "list-multipart-uploads":
        print(json.dumps({positional[-1].split(":", 1)[1]: []}))
    elif cmd == "lsf":
        target = local(positional[-1])
        if target.exists():
            for item in sorted(target.iterdir()):
                if item.is_dir():
                    print(item.name + "/")
    elif cmd == "lsjson":
        target = local(positional[-1])
        items = []
        if target.exists():
            for item in sorted(target.rglob("*")):
                if item.is_file():
                    stamp = datetime.fromtimestamp(item.stat().st_mtime, UTC)
                    items.append({"Path": item.relative_to(target).as_posix(),
                                  "Size": item.stat().st_size,
                                  "ModTime": stamp.isoformat()})
        print(json.dumps(items))
    elif cmd == "cat":
        target = local(positional[-1])
        if not target.is_file():
            sys.exit(3)
        sys.stdout.buffer.write(target.read_bytes())
    elif cmd == "copyto":
        source, target = Path(positional[-2]), local(positional[-1])
        if target.exists() and "--immutable" in rest and \
                target.read_bytes() != source.read_bytes():
            sys.exit(4)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    elif cmd == "rcat":
        target = local(positional[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(sys.stdin.buffer.read())
    elif cmd == "deletefile":
        local(positional[-1]).unlink()
    elif cmd == "rmdir":
        local(positional[-1]).rmdir()
    else:
        sys.exit(9)


if __name__ == "__main__":
    main(sys.argv[1:])
