"""gen-0 migration, generation switch, crash recovery and the startup guard.

Each crash test runs the tool in a real subprocess that SIGKILLs ITSELF at a
named step (DR_GEN_KILL_AT), exactly like a power cut between two renames.
Afterwards ``verify`` must refuse the half state, ``recover`` must finish it,
and the content of the generation must be byte-identical to the original.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/dr/generation_layout.py"
MIGRATE_STEPS = ["after-journal", "after-mkdir", "after-first-move", "after-moves",
                 "after-marker", "before-link-rename", "after-link"]
SWITCH_STEPS = ["switch-after-journal", "before-link-rename", "switch-after-link"]


def run(*args, kill_at=None):
    env = {**os.environ}
    if kill_at:
        env["DR_GEN_KILL_AT"] = kill_at
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=60)


def make_volume(root: Path, tag: str = "A") -> None:
    files = {"part-items/1/photo.jpg": b"jpg" + tag.encode(),
             "part-items/2/photo.jpg": b"jpg2" + tag.encode(),
             "customer_requests/7/scan.pdf": b"pdf" + tag.encode(),
             "catalog-imports/price.bin": os.urandom(4096),
             "readme.txt": b"top-level file"}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)


def tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.name.startswith(".")}


@pytest.fixture
def volume(tmp_path):
    root = tmp_path / "media-volume"
    root.mkdir()
    make_volume(root)
    return root


def test_migrate_gen0_moves_everything_and_links_relatively(volume):
    before = tree(volume)
    result = run("migrate-gen0", volume)
    assert result.returncode == 0, result.stderr
    assert os.readlink(volume / "current") == "gen-0"
    assert tree(volume / "gen-0") == before
    assert run("verify", volume).stdout.strip() == "OK"
    assert run("migrate-gen0", volume).returncode == 1  # never twice


@pytest.mark.parametrize("step", MIGRATE_STEPS)
def test_sigkill_during_migration_is_refused_then_recovered(volume, step):
    before = tree(volume)
    killed = run("migrate-gen0", volume, kill_at=step)
    assert killed.returncode == -signal.SIGKILL
    assert run("verify", volume).returncode == 1  # the half state is never "OK"
    recovered = run("recover", volume)
    assert recovered.returncode == 0, recovered.stderr
    assert run("verify", volume).stdout.strip() == "OK"
    assert tree(volume / "gen-0") == before
    assert run("recover", volume).stdout.strip() == "nothing to recover"  # idempotent


def _second_generation(volume: Path, gen: str = "gen-1") -> Path:
    staged = volume / gen
    staged.mkdir()
    make_volume(staged, tag="B")
    (staged / ".denstock-generation").write_text(gen)
    return staged


@pytest.mark.parametrize("step", SWITCH_STEPS)
def test_sigkill_during_switch_is_recovered_to_the_new_generation(volume, step):
    assert run("migrate-gen0", volume).returncode == 0
    staged = _second_generation(volume)
    expected = tree(staged)
    killed = run("switch", volume, "gen-1", kill_at=step)
    assert killed.returncode == -signal.SIGKILL
    assert run("verify", volume).returncode == 1
    assert run("recover", volume).returncode == 0
    assert os.readlink(volume / "current") == "gen-1"
    assert tree(volume / "current") == expected
    assert run("verify", volume).stdout.strip() == "OK"
    assert tree(volume / "gen-0")  # the old generation is never deleted


def test_switch_refuses_links_missing_marker_and_unknown_generations(volume):
    assert run("migrate-gen0", volume).returncode == 0
    staged = _second_generation(volume)
    (staged / "part-items" / "escape").symlink_to("/etc/hostname")
    refused = run("switch", volume, "gen-1")
    assert refused.returncode == 1 and "symlinks" in refused.stderr
    (staged / "part-items" / "escape").unlink()
    (staged / ".denstock-generation").write_text("gen-9")
    assert "denstock-generation" in run("switch", volume, "gen-1").stderr
    assert run("switch", volume, "gen-404").returncode == 1
    assert run("switch", volume, "../etc").returncode == 1
    assert os.readlink(volume / "current") == "gen-0"


def test_verify_rejects_an_absolute_link(volume):
    assert run("migrate-gen0", volume).returncode == 0
    (volume / "current").unlink()
    (volume / "current").symlink_to(volume / "gen-0")
    assert "relative" in run("verify", volume).stdout


def test_guard_refuses_any_generation_mismatch(tmp_path):
    media, private = tmp_path / "media", tmp_path / "private"
    for root, gens in ((media, ("gen-0", "gen-1")), (private, ("gen-0", "gen-1"))):
        root.mkdir()
        make_volume(root)
        assert run("migrate-gen0", root).returncode == 0
        _second_generation(root)
    assert run("guard", "--db-generation", "gen-0", media, private).stdout.strip() == "OK"
    assert run("switch", media, "gen-1").returncode == 0  # media moved, private did not
    mixed = run("guard", "--db-generation", "gen-0", media, private)
    assert mixed.returncode == 1 and "files are gen-1, database is gen-0" in mixed.stdout
    assert run("switch", private, "gen-1").returncode == 0
    assert run("guard", "--db-generation", "gen-1", media, private).returncode == 0
    assert run("guard", "--db-generation", "gen-0", media, private).returncode == 1


def test_guard_reads_the_database_generation_from_postgresql(tmp_path):
    """The database half: the generation lives in the database comment."""
    url = os.environ.get("DENSTOCK_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL 16 required (DENSTOCK_TEST_DATABASE_URL)")
    import psycopg

    media = tmp_path / "media"
    media.mkdir()
    make_volume(media)
    assert run("migrate-gen0", media).returncode == 0
    with psycopg.connect(url, autocommit=True) as conn:
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
        old = conn.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                           "WHERE datname = current_database()").fetchone()[0]
        try:
            for gen, expect in (("gen-0", 0), ("gen-1", 1)):
                conn.execute(f'COMMENT ON DATABASE "{dbname}" IS \'denstock-generation={gen}\'')
                comment = conn.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                    "WHERE datname = current_database()").fetchone()[0]
                db_gen = comment.split("=", 1)[1]
                assert run("guard", "--db-generation", db_gen, media).returncode == expect
        finally:
            conn.execute(f'COMMENT ON DATABASE "{dbname}" IS ' +
                         ("NULL" if old is None else "'" + old.replace("'", "''") + "'"))
