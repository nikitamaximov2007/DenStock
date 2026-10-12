"""Fault injection for the candidate offsite wrapper (synthetic, no cloud).

The critical invariant: the newest provably restorable remote generation is
never deleted before a newer generation has been uploaded AND confirmed by
read-back. Each test arms one provider fault from ``tests/offsite_harness.py``;
the last test replays a long random sequence of them.
"""

import fcntl
import json
import random
from datetime import datetime, timedelta

import pytest

from tests.offsite_harness import TARGET, OffsiteHarness, _signed_run

OLDEST = "2026-10-07_03-00-00"
PREVIOUS = "2026-10-09_03-00-00"
NEW = "2026-10-11_03-00-12"
LATER = "2026-10-12_03-00-07"
LAST = "2026-10-13_03-00-05"


@pytest.fixture
def generation_bytes(tmp_path):
    run = tmp_path / "size-probe" / NEW
    _signed_run(run, NEW, 40)
    return sum(p.stat().st_size for p in run.rglob("*") if p.is_file())


def _purges(harness):
    return [call for call in harness.rclone_calls() if call[0] == "purge"]


def _cleanups(harness):
    return [call for call in harness.rclone_calls() if call[:2] == ["backend", "cleanup-hidden"]]


def test_local_verification_failure_never_uploads(tmp_path, generation_bytes):
    harness = OffsiteHarness(tmp_path, soft_limit=10 * generation_bytes)
    harness.seed_remote(PREVIOUS)

    result = harness.run(NEW, FAKE_VERIFY_FAIL=NEW)

    assert result.returncode == 1
    assert result.status["status"] == "failed"
    assert not any(call[0] == "copy" for call in harness.rclone_calls())
    assert harness.restorable_runs() == [PREVIOUS]


def test_local_change_after_verification_blocks_rotation(tmp_path, generation_bytes):
    """Read-back equality alone is not a signature check (regression).

    The local run changes after the first verify_backup; the upload copies the
    changed bytes, so ``rclone check`` passes. Only a second verification of
    the local reference proves the remote bytes are signed bytes.
    """
    harness = OffsiteHarness(tmp_path, soft_limit=3 * generation_bytes + 10)
    harness.seed_remote(OLDEST)
    harness.seed_remote(PREVIOUS)
    harness.arm(tamper_local_on_copy=True)

    result = harness.run(NEW)

    assert result.returncode == 1
    assert result.status["status"] == "failed"
    assert _purges(harness) == []
    assert harness.restorable_runs() == [OLDEST, PREVIOUS]
    assert harness.docker_verify_calls() == [NEW, NEW]


def test_targeted_cleanup_frees_only_the_purged_generation(tmp_path, generation_bytes):
    harness = OffsiteHarness(tmp_path, soft_limit=3 * generation_bytes + 10)
    harness.seed_remote(OLDEST)
    harness.seed_remote(PREVIOUS)
    harness.put_foreign("other-namespace/old.bin", 5, hidden=True)
    harness.put_foreign("denstock-archive/keep.bin", 7, hidden=True)

    result = harness.run(NEW)

    assert result.returncode == 0, result.stderr
    assert result.status["status"] == "ok"
    assert harness.restorable_runs() == [PREVIOUS, NEW]
    assert _purges(harness) == [["purge", f"{TARGET}/{OLDEST}"]]
    assert _cleanups(harness) == [["backend", "cleanup-hidden", f"{TARGET}/{OLDEST}"]]
    hidden = {item["key"] for item in harness.state()["hidden"]}
    assert hidden == {"other-namespace/old.bin", "denstock-archive/keep.bin"}


def test_failed_hidden_cleanup_keeps_new_and_previous_and_stops(tmp_path, generation_bytes):
    harness = OffsiteHarness(tmp_path, soft_limit=3 * generation_bytes + 10)
    harness.seed_remote(OLDEST)
    harness.seed_remote(PREVIOUS)
    harness.arm(cleanup_hidden_fail=True)

    result = harness.run(NEW)

    assert result.returncode == 1
    assert result.status["status"] == "failed"
    assert harness.restorable_runs() == [PREVIOUS, NEW]
    # The purged generation is now hidden-only: invisible to lsf, still counted.
    assert OLDEST not in harness.remote_runs()
    assert any(item["key"].startswith(f"denstock/{OLDEST}/")
               for item in harness.state()["hidden"])

    harness.disarm()
    later = harness.run(LATER)

    # Documented gap: the wrapper never reclaims a hidden-only generation; it
    # refuses the next upload instead of deleting anything verified.
    assert later.returncode == 1
    assert later.status["status"] == "failed"
    assert not any(call[0] in {"copy", "purge"} for call in harness.rclone_calls())
    assert harness.restorable_runs() == [PREVIOUS, NEW]


def test_listing_that_omits_the_fresh_generation_fails_closed(tmp_path, generation_bytes):
    harness = OffsiteHarness(tmp_path, soft_limit=3 * generation_bytes + 10)
    harness.seed_remote(OLDEST)
    harness.seed_remote(PREVIOUS)
    harness.arm(lsf_omit_next={"names": [NEW], "times": 2})

    result = harness.run(NEW)

    assert result.returncode == 1
    assert result.status["status"] == "failed"
    assert _purges(harness) == []
    assert harness.restorable_runs() == [OLDEST, PREVIOUS, NEW]

    # Documented liveness gap: the failed run consumed the one-generation
    # headroom and never rotated; uploads are refused (never pre-deleted)
    # until an operator rotates by hand. The data stays safe, RPO grows.
    later = harness.run(LATER)
    assert later.returncode == 1
    assert "Недостаточно" in later.status["message"]
    assert not any(call[0] in {"copy", "purge"} for call in harness.rclone_calls())
    assert harness.restorable_runs() == [OLDEST, PREVIOUS, NEW]


def test_listing_that_hides_an_old_generation_never_deletes_it(tmp_path, generation_bytes):
    harness = OffsiteHarness(tmp_path, soft_limit=4 * generation_bytes + 10)
    harness.seed_remote("2026-10-05_03-00-00")
    harness.seed_remote(OLDEST)
    harness.seed_remote(PREVIOUS)
    harness.arm(lsf_omit_next={"names": ["2026-10-05_03-00-00"], "times": 10})

    result = harness.run(NEW)

    assert "2026-10-05_03-00-00" in harness.restorable_runs()
    assert all(not call[1].endswith("2026-10-05_03-00-00") for call in _purges(harness))
    assert PREVIOUS in harness.restorable_runs() and NEW in harness.restorable_runs()
    assert result.status["status"] in {"ok", "failed", "degraded"}


def test_provider_accounting_lag_refuses_upload_without_deleting(tmp_path, generation_bytes):
    """Purged bytes stay counted by the provider (Yandex: up to ~20 minutes).

    A release-day POST backup right after a rotating run is refused by the
    provider mid-upload although the listing-based soft limit allowed it.
    """
    limit = 3 * generation_bytes + 50
    harness = OffsiteHarness(tmp_path, soft_limit=limit, quota=limit)
    harness.seed_remote(OLDEST)
    harness.seed_remote(PREVIOUS)

    first = harness.run(NEW)
    assert first.returncode == 0, first.stderr
    assert harness.state()["lagged_freed"] == generation_bytes

    second = harness.run(LATER)  # minutes later, accounting not settled
    assert second.returncode == 1
    assert second.status["status"] == "failed"
    assert _purges(harness) == []
    assert harness.restorable_runs() == [PREVIOUS, NEW]
    assert LATER in harness.remote_runs()  # a partial generation stays behind

    harness.settle()
    third = harness.run(LAST)
    # Documented liveness gap: the newest remote name is the partial run, so
    # it is the "previous" generation, fails read-back, and rotation stops.
    assert third.returncode == 1
    assert third.status["status"] == "degraded"
    assert _purges(harness) == []
    assert harness.restorable_runs() == [PREVIOUS, NEW, LAST]


def test_enospc_mid_upload_keeps_every_verified_generation(tmp_path, generation_bytes):
    harness = OffsiteHarness(tmp_path, soft_limit=3 * generation_bytes + 10)
    harness.seed_remote(OLDEST)
    harness.seed_remote(PREVIOUS)
    harness.arm(copy_enospc=1)

    result = harness.run(NEW)

    assert result.returncode == 1
    assert result.status["status"] == "failed"
    assert "no space left" in result.stderr
    assert _purges(harness) == []
    assert harness.restorable_runs() == [OLDEST, PREVIOUS]


def test_interrupted_upload_does_not_leave_a_stale_ok_status(tmp_path, generation_bytes):
    """systemd stops the unit mid-upload (regression).

    Before the EXIT trap the status file kept the previous night's "ok".
    """
    harness = OffsiteHarness(tmp_path, soft_limit=10 * generation_bytes)
    harness.seed_remote(PREVIOUS)
    assert harness.run(NEW).status["status"] == "ok"
    harness.arm(copy_sigterm_after=1)

    result = harness.run(LATER)

    assert result.returncode == 143
    assert result.status["status"] == "failed"
    assert result.status["uploaded_run"] == ""
    assert result.status["local_backup_run"] == LATER
    assert _purges(harness) == []
    assert harness.restorable_runs() == [PREVIOUS, NEW]


def test_status_is_running_while_the_wrapper_works(tmp_path, generation_bytes):
    harness = OffsiteHarness(tmp_path, soft_limit=10 * generation_bytes)
    harness.arm(copy_sigterm_after=0)
    real = harness.bin / "rclone"
    body = real.read_text()
    real.write_text(body.replace(
        'command = args[0]\n',
        'command = args[0]\n'
        'if command == "copy":\n'
        '    status = pathlib.Path(os.environ["FAKE_PROJECT"]) / "backups/offsite_status.json"\n'
        '    (remote / "status-during-copy.json").write_text(status.read_text())\n',
    ))

    harness.run(NEW)
    seen = json.loads((harness.remote / "status-during-copy.json").read_text())

    assert seen["status"] == "running"
    assert seen["uploaded_run"] == ""


def test_incomplete_multipart_upload_is_invisible_to_the_soft_limit(tmp_path, generation_bytes):
    harness = OffsiteHarness(
        tmp_path, soft_limit=2 * generation_bytes + 30, quota=2 * generation_bytes + 15
    )
    harness.seed_remote(PREVIOUS)
    harness.arm(copy_fail_after=0, multipart_cutoff=10)

    first = harness.run(NEW)
    assert first.returncode == 1
    pending = harness.state()["multipart"]
    assert len(pending) == 1 and pending[0]["size"] == 20

    harness.disarm()
    second = harness.run(LATER)

    # The wrapper's listing does not see the 20 pending bytes; the provider
    # does and refuses the upload. Nothing verified is deleted either way.
    assert second.returncode == 1
    assert second.status["status"] == "failed"
    assert _purges(harness) == []
    assert harness.restorable_runs() == [PREVIOUS]


def test_lock_held_elsewhere_leaves_status_untouched(tmp_path, generation_bytes):
    harness = OffsiteHarness(tmp_path, soft_limit=10 * generation_bytes)
    harness.seed_remote(PREVIOUS)
    assert harness.run(NEW).status["status"] == "ok"
    before = (harness.root / "backups/offsite_status.json").read_text()

    with open(tmp_path / "backup.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = harness.run(LATER)

    assert result.returncode == 75
    assert (harness.root / "backups/offsite_status.json").read_text() == before
    assert harness.rclone_calls() == []


FAULTS = [
    {},
    {},
    {},
    {"copy_fail_after": 1},
    {"copy_enospc": 0},
    {"cleanup_hidden_fail": True},
    {"check_fail": True},
    {"tamper_local_on_copy": True},
    {"copy_sigterm_after": 1},
    {"lsf_omit_next": {"names": ["__NEW__"], "times": 2}},
]


@pytest.mark.parametrize("seed", [3, 11, 2026])
def test_newest_restorable_generation_survives_random_faults(tmp_path, generation_bytes, seed):
    rng = random.Random(seed)
    limit = 5 * generation_bytes + 60
    harness = OffsiteHarness(tmp_path, soft_limit=limit, quota=limit + generation_bytes)
    harness.seed_remote(OLDEST)
    start = datetime(2026, 10, 10, 3, 0, 0)
    outcomes = set()
    for step in range(30):
        run_id = (start + timedelta(hours=8 * step)).strftime("%Y-%m-%d_%H-%M-%S")
        before = harness.restorable_runs()
        fault = json.loads(json.dumps(rng.choice(FAULTS)).replace("__NEW__", run_id))
        harness.disarm()
        harness.arm(**fault)
        if rng.random() < 0.7:
            harness.settle()

        result = harness.run(run_id)
        after = harness.restorable_runs()
        outcomes.add(result.status["status"])

        assert result.status["status"] in {"ok", "failed", "degraded"}, (step, fault)
        if before:
            assert before[-1] in after or run_id in after, (step, fault, before, after)
        purges = _purges(harness)
        if purges:
            assert run_id in after, (step, fault)
            calls = harness.rclone_calls()
            first_purge = calls.index(purges[0])
            assert ["check", "--download", f"backups/{run_id}", f"{TARGET}/{run_id}"] in (
                calls[:first_purge]
            )
            assert harness.docker_verify_calls().count(run_id) >= 2
    # Outcomes differ per seed: one unlucky fault can stall the wrapper in the
    # documented fail-closed state for the rest of the sequence.
    assert outcomes <= {"ok", "failed", "degraded"}
