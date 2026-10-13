#!/usr/bin/env python3
"""Generation layout inside a media volume (docs/design/dr-generation-storage.md, A).

    generation_layout.py migrate-gen0 <volume root>     one-time, writers stopped
    generation_layout.py recover <volume root>          after a crash: roll forward
    generation_layout.py verify <volume root>           read-only check, exit 1 if bad
    generation_layout.py switch <volume root> <gen-id>  atomic, writers stopped
    generation_layout.py guard --db-generation G <media root> <private root>

Layout: ``<root>/gen-<id>/`` (with ``.denstock-generation``) and a RELATIVE
symlink ``<root>/current -> gen-<id>``. Every step is a rename inside one
filesystem and is journalled in ``<root>/.denstock-generation-journal`` before
it happens, so a SIGKILL or power loss at any point leaves a state that
``recover`` completes and ``verify``/``guard`` refuse until then. Nothing here
deletes data. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

JOURNAL = ".denstock-generation-journal"
MARKER = ".denstock-generation"
CURRENT = "current"


class LayoutError(Exception):
    pass


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durable(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fault(step: str) -> None:
    """Test hook: DR_GEN_KILL_AT=<step> kills this process with SIGKILL there."""
    if os.environ.get("DR_GEN_KILL_AT") == step:
        os.kill(os.getpid(), signal.SIGKILL)


def _valid_id(gen_id: str) -> bool:
    return gen_id.startswith("gen-") and gen_id[4:].replace("-", "").isalnum() and "/" not in gen_id


def _link(root: Path, gen_id: str) -> None:
    """Atomically point ``current`` at ``gen_id`` (relative target)."""
    tmp = root / f".{CURRENT}.{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(gen_id, tmp)
    _fault("before-link-rename")
    os.replace(tmp, root / CURRENT)
    _fsync_dir(root)


def _user_entries(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir()
                  if not p.name.startswith("gen-") and p.name not in (CURRENT, JOURNAL)
                  and not p.name.startswith(f".{CURRENT}."))


def migrate_gen0(root: Path) -> None:
    if (root / CURRENT).is_symlink():
        raise LayoutError("already migrated: current exists")
    entries = [p.name for p in _user_entries(root)]
    _write_durable(root / JOURNAL, json.dumps({"op": "migrate-gen0", "target": "gen-0",
                                               "entries": entries}))
    _fault("after-journal")
    _roll_forward(root, "gen-0", entries)


def _roll_forward(root: Path, gen_id: str, entries: list[str]) -> None:
    gen = root / gen_id
    if not gen.exists():
        gen.mkdir()
        _fsync_dir(root)
    _fault("after-mkdir")
    for index, name in enumerate(entries):
        source, target = root / name, gen / name
        if source.exists() or source.is_symlink():
            if target.exists() or target.is_symlink():
                raise LayoutError(f"both {name} and {gen_id}/{name} exist: manual review")
            os.rename(source, target)  # same filesystem: atomic per entry
            _fsync_dir(root)
            _fsync_dir(gen)
        if index == 0:
            _fault("after-first-move")
    _fault("after-moves")
    _write_durable(gen / MARKER, gen_id)
    _fault("after-marker")
    _link(root, gen_id)
    _fault("after-link")
    (root / JOURNAL).unlink()
    _fsync_dir(root)


def recover(root: Path) -> str:
    journal = root / JOURNAL
    for tmp in root.glob(f".{CURRENT}.*"):
        tmp.unlink()  # an unrenamed new link: never visible to readers
    if not journal.exists():
        return "nothing to recover"
    state = json.loads(journal.read_text())
    if state.get("op") == "migrate-gen0":
        _roll_forward(root, state["target"], state["entries"])
        return "migrate-gen0 completed"
    if state.get("op") == "switch":
        # The switch is one rename: either it happened or not. Finish it.
        _link(root, state["target"])
        journal.unlink()
        _fsync_dir(root)
        return f"switch to {state['target']} completed"
    raise LayoutError("unknown journal: manual review")


def links_inside(gen: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(gen):
        for name in dirnames + filenames:
            if os.path.islink(os.path.join(dirpath, name)):
                found.append(os.path.relpath(os.path.join(dirpath, name), gen))
    return found


def verify(root: Path) -> list[str]:
    problems = []
    if (root / JOURNAL).exists():
        problems.append("journal present: run recover")
    current = root / CURRENT
    if not current.is_symlink():
        return problems + ["current is not a symlink"]
    target = os.readlink(current)
    if os.path.isabs(target) or not _valid_id(target):
        problems.append(f"current target must be a relative gen-<id>, got {target!r}")
        return problems
    gen = root / target
    if not gen.is_dir():
        return problems + [f"current points at missing {target}"]
    marker = gen / MARKER
    if not marker.exists() or marker.read_text().strip() != target:
        problems.append(f"{target} has no matching {MARKER}")
    if links_inside(gen):
        problems.append(f"{target} contains symlinks: {len(links_inside(gen))}")
    stray = [p.name for p in _user_entries(root) if not p.name.startswith(".")]
    if stray:
        problems.append(f"entries outside any generation: {len(stray)}")
    return problems


def switch(root: Path, gen_id: str) -> None:
    if not _valid_id(gen_id) or not (root / gen_id).is_dir():
        raise LayoutError(f"{gen_id} is not a generation here")
    problems = verify(root)
    if problems:
        raise LayoutError("current layout is not clean: " + "; ".join(problems))
    if links_inside(root / gen_id):
        raise LayoutError(f"{gen_id} contains symlinks: refusing to serve it")
    marker = root / gen_id / MARKER
    if not marker.exists() or marker.read_text().strip() != gen_id:
        raise LayoutError(f"{gen_id} has no matching {MARKER}")
    _write_durable(root / JOURNAL, json.dumps({"op": "switch", "target": gen_id}))
    _fault("switch-after-journal")
    _link(root, gen_id)
    _fault("switch-after-link")
    (root / JOURNAL).unlink()
    _fsync_dir(root)


def guard(db_generation: str, *roots: Path) -> list[str]:
    """Startup guard: database and every file store must be the SAME generation."""
    problems = []
    for root in roots:
        problems += [f"{root}: {p}" for p in verify(root)]
        current = root / CURRENT
        if current.is_symlink():
            gen = os.readlink(current)
            if gen != db_generation:
                problems.append(f"{root}: files are {gen}, database is {db_generation}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("migrate-gen0", "recover", "verify"):
        sub.add_parser(name).add_argument("root", type=Path)
    sw = sub.add_parser("switch")
    sw.add_argument("root", type=Path)
    sw.add_argument("gen_id")
    gd = sub.add_parser("guard")
    gd.add_argument("--db-generation", required=True)
    gd.add_argument("roots", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "migrate-gen0":
            migrate_gen0(args.root)
        elif args.cmd == "recover":
            print(recover(args.root))
        elif args.cmd == "verify":
            problems = verify(args.root)
            print("\n".join(problems) or "OK")
            return 1 if problems else 0
        elif args.cmd == "switch":
            switch(args.root, args.gen_id)
        else:
            problems = guard(args.db_generation, *args.roots)
            print("\n".join(problems) or "OK")
            return 1 if problems else 0
    except LayoutError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
