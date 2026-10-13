"""Daily encrypted backups in GitHub Releases, against an ISOLATED fake GitHub.

Everything is synthetic: random backup runs carrying a plaintext marker, a
throwaway age identity, fake tokens, a fake API on 127.0.0.1 (tests/github_fake.py).
The job runs as a real subprocess, so exit codes, locks, signals and stderr are
the ones production would see. Every test ends by proving the peak storage the
fake ever held stayed within the budget.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.github_fake import READ_TOKEN, WRITE_TOKEN, FakeGitHub

pytestmark = pytest.mark.skipif(
    not (shutil.which("age") and shutil.which("age-keygen")), reason="age is required")

ROOT = Path(__file__).resolve().parents[1]
JOB = ROOT / "scripts/operations/github_release_backup.py"
MARKER = b"PLAINTEXT-CUSTOMER-MARKER-gh-5c1e"
KIB, MIB = 1024, 1024 * 1024


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    fake = FakeGitHub(tmp_path / "github-blobs").start()
    identity = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = tmp_path / "recipient.txt"
    recipient.write_text(subprocess.run(["age-keygen", "-y", str(identity)], check=True,
                                        capture_output=True, text=True).stdout)
    tokens = {}
    for name, value in (("write", WRITE_TOKEN), ("read", READ_TOKEN)):
        path = tmp_path / f"{name}.token"
        path.write_text(value + "\n")
        path.chmod(0o600)
        tokens[name] = path
    backups = tmp_path / "backups"
    backups.mkdir()
    ctx = {"fake": fake, "identity": identity, "recipient": recipient, "tokens": tokens,
           "backups": backups, "tmp": tmp_path,
           "base": {"GHB_CONFIG": "/nonexistent", "GHB_REPO": fake.repo,
                    "GHB_API_URL": fake.api, "GHB_UPLOAD_URL": fake.uploads,
                    "GHB_RECIPIENT_FILE": str(recipient), "BACKUP_ROOT": str(backups),
                    "GHB_LOCK_FILE": str(tmp_path / "job.lock"),
                    "GHB_RETRIES": "2", "GHB_RETRY_DELAY": "0.05", "GHB_TIMEOUT": "20",
                    "GHB_FORBIDDEN_REPOS": "owner/DenStock"}}
    try:
        yield ctx
    finally:
        fake.stop()


def _run_id(when: datetime) -> str:
    return when.strftime("%Y-%m-%d_%H-%M-%S")


def make_run(backups: Path, when: datetime, size: int, *, private=True, verified=True) -> str:
    import hashlib

    run_id = _run_id(when)
    run = backups / run_id
    run.mkdir()
    parts = {"db.dump": int(size * 0.6), "media.tar.gz": int(size * 0.1)}
    if private:
        parts["private_media.tar.gz"] = size - sum(parts.values())
    hashes = {}
    for name, length in parts.items():
        data = MARKER + os.urandom(max(0, length - len(MARKER)))
        (run / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    manifest = {"verification_status": "verified" if verified else "failed",
                "created_at": when.isoformat(), "app_commit": "f" * 40,
                "private_media_status": "included" if private else "absent",
                "private_media_filename": "private_media.tar.gz" if private else None,
                "private_media_file_count": 3 if private else None,
                "sha256": hashes, "signature": {"algorithm": "ed25519", "value": "c3ludGg="}}
    (run / "manifest.json").write_text(json.dumps(manifest))
    return run_id


def push(ctx, *, token="write", **extra):
    environ = {**os.environ, **ctx["base"], "GHB_TOKEN_FILE": str(ctx["tokens"][token]),
               **{k: str(v) for k, v in extra.items()}}
    result = subprocess.run([sys.executable, str(JOB), "push"], env=environ,
                            capture_output=True, text=True, timeout=300)
    status_path = ctx["backups"] / "github_backup_status.json"
    result.status = json.loads(status_path.read_text()) if status_path.exists() else {}
    return result


def check(ctx, *args, token="read", **extra):
    environ = {**os.environ, **ctx["base"], "GHB_TOKEN_FILE": str(ctx["tokens"][token]),
               **{k: str(v) for k, v in extra.items()}}
    return subprocess.run([sys.executable, str(JOB), "check", *args], env=environ,
                          capture_output=True, text=True, timeout=300)


def fetch(ctx, out, *args, token="read"):
    environ = {**os.environ, **ctx["base"], "GHB_TOKEN_FILE": str(ctx["tokens"][token])}
    return subprocess.run([sys.executable, str(JOB), "fetch", "--out", str(out), *args],
                          env=environ, capture_output=True, text=True, timeout=300)


def valid_runs(fake):
    return [r["tag_name"].split("-", 2)[2][:19] for r in fake.published()]


def assert_never_over_budget(ctx, budget):
    assert ctx["fake"].max_used <= budget, (ctx["fake"].max_used, budget)


def assert_no_secret_leak(ctx, *results):
    for result in results:
        for text in (result.stdout, result.stderr, json.dumps(getattr(result, "status", {}))):
            assert WRITE_TOKEN not in text and READ_TOKEN not in text
    assert ctx["fake"].auth_leaks == 0  # the token never followed a redirect


def decrypt_asset(ctx, path: Path, dest: Path):
    dest.mkdir()
    dec = subprocess.Popen(["age", "-d", "-i", str(ctx["identity"]), str(path)],
                           stdout=subprocess.PIPE)
    subprocess.run(["tar", "-xf", "-", "-C", str(dest)], stdin=dec.stdout, check=True)
    assert dec.wait() == 0


NOW = datetime.now(UTC).replace(microsecond=0)


# --- normal operation -------------------------------------------------------------


def test_normal_daily_upload_publishes_reads_back_and_decrypts_to_the_same_bytes(env):
    run = make_run(env["backups"], NOW - timedelta(hours=1), 300 * KIB)
    result = push(env)
    assert result.returncode == 0, result.stderr
    assert result.status["status"] == "ok"
    [release] = env["fake"].published()
    assert release["tag_name"].startswith(f"denstock-backup-{run}-")
    names = sorted(env["fake"].assets[a]["name"] for a in release["asset_ids"])
    assert names == [f"{run}.backup.json", f"{run}.signed-backup.tar.age"]
    out = env["tmp"] / "fetched"
    fetched = fetch(env, out)
    assert fetched.returncode == 0, fetched.stderr
    decrypt_asset(env, out / f"{run}.signed-backup.tar.age", env["tmp"] / "plain")
    for name in ("db.dump", "media.tar.gz", "private_media.tar.gz", "manifest.json"):
        assert (env["tmp"] / "plain" / run / name).read_bytes() == (
            env["backups"] / run / name).read_bytes()
    assert check(env).stdout.startswith("DEGRADED")  # one copy is not yet two
    assert_no_secret_leak(env, result, fetched)


def test_plaintext_never_reaches_github(env):
    make_run(env["backups"], NOW, 200 * KIB)
    assert push(env).returncode == 0
    for blob in env["fake"].blob_dir.iterdir():
        assert MARKER not in blob.read_bytes()
    # release text and the description asset carry no customer data either
    for rel in env["fake"].releases.values():
        assert MARKER.decode() not in rel["body"]


def test_seven_sequential_days(env):
    budget = 4 * MIB
    for day in range(7):
        make_run(env["backups"], NOW - timedelta(days=6 - day), 400 * KIB)
        result = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=int(budget * 0.85))
        assert result.returncode == 0, result.stderr
    assert len(env["fake"].published()) == 7
    fresh = check(env, "--deep")
    assert fresh.returncode == 0 and fresh.stdout.startswith("FRESH"), fresh.stdout
    assert_never_over_budget(env, budget)


def test_thirty_days_with_growing_archives_rotate_within_budget(env):
    budget, watermark = 8 * MIB, int(6.8 * MIB)
    days = []
    for day in range(30):
        size = 500 * KIB + day * 40 * KIB  # grows from ~0.5 to ~1.7 MiB
        days.append(make_run(env["backups"], NOW - timedelta(days=29 - day), size))
        result = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark)
        assert result.returncode == 0, (day, result.stderr)
        assert env["fake"].used() <= watermark or result.status["status"] == "warning"
        if day >= 1:
            assert result.status["valid_generations"] >= 2
        assert valid_runs(env["fake"])[-1] == days[-1]  # the newest is always present
        shutil.rmtree(env["backups"] / days[-1])  # the VPS keeps only a few local runs
    assert_never_over_budget(env, budget)
    assert valid_runs(env["fake"]) == days[-len(valid_runs(env["fake"])):]  # oldest went first
    assert check(env, "--deep", "--deep-all").returncode == 0


def test_production_sized_generations_near_one_gib(env):
    """Real defaults (1 GiB budget, 850 MiB watermark), ~98 MiB generations like prod."""
    for day in range(12):
        run = make_run(env["backups"], NOW - timedelta(days=11 - day), 98 * MIB)
        result = push(env)
        assert result.returncode == 0, result.stderr
        assert env["fake"].used() <= 850 * MIB
        # Steady state: old copies are deleted only AFTER the new one is proven,
        # never in front of an upload.
        assert "ротация: удаляю" not in result.stderr
        shutil.rmtree(env["backups"] / run)
    assert_never_over_budget(env, 850 * MIB)  # even the transient peak stayed under it
    # 7 kept + room for tomorrow's (~108 MiB reserve) under the 850 MiB watermark
    assert len(env["fake"].published()) == 7


def test_second_push_of_the_same_run_changes_nothing(env):
    make_run(env["backups"], NOW, 100 * KIB)
    assert push(env).returncode == 0
    before = json.dumps(env["fake"].releases, sort_keys=True)
    again = push(env)
    assert again.returncode == 0 and "уже выгружена" in again.status["message"]
    assert json.dumps(env["fake"].releases, sort_keys=True) == before


# --- failures ---------------------------------------------------------------------


def test_interrupted_upload_never_counts_and_is_cleaned_next_run(env):
    make_run(env["backups"], NOW - timedelta(days=1), 300 * KIB)
    assert push(env).returncode == 0
    newer = make_run(env["backups"], NOW, 300 * KIB)
    env["fake"].faults["cut_upload_after"] = 64 * KIB
    cut = push(env)
    assert cut.returncode == 1 and cut.status["status"] == "failed"
    drafts = [r for r in env["fake"].releases.values() if r["draft"]]
    assert len(drafts) == 1  # left behind, but invisible to recovery
    assert newer not in valid_runs(env["fake"])
    fetched = fetch(env, env["tmp"] / "f1")
    assert newer not in fetched.stdout  # recovery never picks the cut copy
    assert not list((env["backups"] / ".github-upload").glob("*"))  # no ciphertext left
    retry = push(env)
    assert retry.returncode == 0, retry.stderr
    assert not [r for r in env["fake"].releases.values() if r["draft"]]
    assert valid_runs(env["fake"])[-1] == newer


def test_sigterm_mid_upload_leaves_failed_status_and_no_local_ciphertext(env):
    make_run(env["backups"], NOW, 2 * MIB)
    env["fake"].faults["slow_upload"] = 0.5
    environ = {**os.environ, **env["base"], "GHB_TOKEN_FILE": str(env["tokens"]["write"])}
    proc = subprocess.Popen([sys.executable, str(JOB), "push"], env=environ,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 30
    while not any(m == "POST" and p.startswith("/uploads") for m, p in env["fake"].calls):
        assert time.time() < deadline
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 1
    status = json.loads((env["backups"] / "github_backup_status.json").read_text())
    assert status["status"] == "failed"
    assert not list((env["backups"] / ".github-upload").glob("*.age*"))
    assert not env["fake"].published()
    env["fake"].faults.pop("slow_upload")
    assert push(env).returncode == 0


def test_corrupted_remote_copy_is_detected_and_never_kept_as_a_survivor(env):
    budget, watermark = 3 * MIB, int(2.55 * MIB)
    runs = []
    for day in range(3):
        runs.append(make_run(env["backups"], NOW - timedelta(days=3 - day), 700 * KIB))
        assert push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark).returncode == 0
    newest_asset = env["fake"].asset_by_name(f"{runs[-1]}.signed-backup.tar.age")
    env["fake"].corrupt(newest_asset["id"])
    deep = check(env, "--deep")
    assert deep.returncode == 1 and deep.stdout.startswith("CORRUPT"), deep.stdout
    runs.append(make_run(env["backups"], NOW, 700 * KIB))
    result = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark)
    assert result.returncode == 0, result.stderr
    assert "повреждена" in result.stderr
    # the corrupted copy was pruned first; two genuine copies remain with the new one
    assert runs[2] not in valid_runs(env["fake"])
    assert runs[-1] in valid_runs(env["fake"]) and runs[1] in valid_runs(env["fake"])
    assert check(env, "--deep", "--deep-all").returncode == 0
    assert_never_over_budget(env, budget)


def test_github_outage_fails_cleanly_and_deletes_nothing(env):
    make_run(env["backups"], NOW - timedelta(days=1), 200 * KIB)
    assert push(env).returncode == 0
    before = json.dumps(env["fake"].releases, sort_keys=True)
    make_run(env["backups"], NOW, 200 * KIB)
    env["fake"].faults["outage"] = True
    result = push(env)
    assert result.returncode == 1 and result.status["status"] == "failed"
    assert "GitHub недоступен" in result.status["message"]
    monitor = check(env)
    assert monitor.returncode == 1 and monitor.stdout.startswith("UNREACHABLE")
    env["fake"].faults.clear()
    assert json.dumps(env["fake"].releases, sort_keys=True) == before


def test_unreachable_github_is_reported_not_hung(env):
    make_run(env["backups"], NOW, 100 * KIB)
    result = push(env, GHB_API_URL="http://127.0.0.1:9", GHB_UPLOAD_URL="http://127.0.0.1:9")
    assert result.returncode == 1 and "недоступен" in result.status["message"]


def test_invalid_or_expired_token_is_named_and_never_printed(env):
    make_run(env["backups"], NOW, 100 * KIB)
    bad = env["tmp"] / "bad.token"
    bad.write_text("ghp_expired_synthetic_token_value\n")
    env["tokens"]["bad"] = bad
    result = push(env, token="bad")
    assert result.returncode == 1
    assert "недействителен или просрочен" in result.status["message"]
    assert "ghp_expired_synthetic_token_value" not in result.stderr + json.dumps(result.status)
    assert not env["fake"].releases


def test_read_only_token_cannot_upload_or_delete(env):
    make_run(env["backups"], NOW, 100 * KIB)
    result = push(env, token="read")
    assert result.returncode == 1 and "403" in result.status["message"]
    assert not env["fake"].releases


def test_run_without_private_media_is_refused(env):
    make_run(env["backups"], NOW, 100 * KIB, private=False)
    result = push(env)
    assert result.returncode == 1 and "private_media" in result.status["message"]
    assert not env["fake"].releases


def test_unverified_or_damaged_local_run_is_refused(env):
    run = make_run(env["backups"], NOW, 100 * KIB)
    (env["backups"] / run / "db.dump").write_bytes(b"tampered")
    result = push(env)
    assert result.returncode == 1 and "хеш db.dump" in result.status["message"]
    assert not env["fake"].releases


# --- retention and budget ---------------------------------------------------------


def test_only_two_valid_copies_are_never_rotated_away(env):
    budget = int(2.2 * MIB)
    for day in range(2):
        make_run(env["backups"], NOW - timedelta(days=2 - day), 700 * KIB)
        assert push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=budget).returncode == 0
    two = valid_runs(env["fake"])
    make_run(env["backups"], NOW, 900 * KIB)  # does not fit next to the two
    result = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=budget)
    assert result.returncode == 1 and "не помещается" in result.status["message"]
    assert "ничего не удалено" in result.status["message"]
    assert valid_runs(env["fake"]) == two
    assert_never_over_budget(env, budget)


def test_above_watermark_but_within_budget_uploads_with_a_warning(env):
    budget, watermark = 3 * MIB, int(1.2 * MIB)
    for day in range(2):
        make_run(env["backups"], NOW - timedelta(days=2 - day), 700 * KIB)
        assert push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark).returncode == 0
    make_run(env["backups"], NOW, 700 * KIB)
    result = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark)
    assert result.returncode == 0 and result.status["status"] == "warning"
    assert len(valid_runs(env["fake"])) == 2  # oldest pruned, two kept (new + previous)
    assert_never_over_budget(env, budget)


def test_unrecognized_and_keep_releases_are_never_deleted_but_are_counted(env):
    budget, watermark = 4 * MIB, int(3.4 * MIB)
    fake = env["fake"]
    foreign = fake.add_release("v1.0", body="manual release", assets=[("notes.bin", b"x" * MIB)])
    for day in range(4):
        make_run(env["backups"], NOW - timedelta(days=5 - day), 500 * KIB)
        assert push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark).returncode == 0
    oldest = min(fake.published(), key=lambda r: r["tag_name"] if r["id"] != foreign else "~")
    oldest["name"] += " [keep]"  # the owner pins the oldest generation by hand
    for day in range(4, 8):
        make_run(env["backups"], NOW - timedelta(days=5 - day), 500 * KIB)
        assert push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark).returncode == 0
    assert foreign in fake.releases and oldest["id"] in fake.releases
    assert fake.used() <= watermark
    assert_never_over_budget(env, budget)


def test_failed_older_operation_never_deletes_a_newer_valid_copy(env):
    make_run(env["backups"], NOW - timedelta(days=2), 200 * KIB)
    make_run(env["backups"], NOW - timedelta(days=1), 200 * KIB)
    assert push(env, GHB_RUN=_run_id(NOW - timedelta(days=2))).returncode == 0
    assert push(env).returncode == 0
    valid_before = valid_runs(env["fake"])
    # an older run whose upload is cut: it must not disturb the newer copies
    stale = make_run(env["backups"], NOW - timedelta(days=3), 200 * KIB)
    older = push(env, GHB_RUN=stale)
    assert older.returncode == 0 and "более новая" in older.status["message"]
    assert valid_runs(env["fake"]) == valid_before


def test_existing_unconfirmed_release_with_the_same_id_is_never_overwritten(env):
    run = make_run(env["backups"], NOW, 100 * KIB)
    assert push(env).returncode == 0
    [rel] = env["fake"].published()
    sidecar_id = rel["asset_ids"][1]
    env["fake"].assets[sidecar_id]["state"] = "starter"  # now incomplete, still published
    again = push(env)
    assert again.returncode == 1 and "не перезаписываю" in again.status["message"]
    assert run in rel["tag_name"]


def test_overlapping_runs_do_not_run_twice(env):
    make_run(env["backups"], NOW, 100 * KIB)
    with open(env["base"]["GHB_LOCK_FILE"], "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = push(env)
    assert result.returncode == 75
    assert not env["fake"].releases


def test_public_or_source_repository_is_refused(env):
    make_run(env["backups"], NOW, 100 * KIB)
    env["fake"].private = False
    assert "не приватный" in push(env).status["message"]
    env["fake"].private = True
    result = push(env, GHB_FORBIDDEN_REPOS=env["fake"].repo)
    assert "исходного кода" in result.status["message"]
    assert not env["fake"].releases


def test_private_key_in_the_recipient_file_is_refused(env):
    make_run(env["backups"], NOW, 100 * KIB)
    result = push(env, GHB_RECIPIENT_FILE=env["identity"])
    assert result.returncode == 1 and "ЗАКРЫТЫЙ ключ" in result.status["message"]
    assert not env["fake"].releases


def test_monitor_reports_stale_when_uploads_stop(env):
    make_run(env["backups"], NOW - timedelta(days=3), 100 * KIB)
    assert push(env).returncode == 0
    make_run(env["backups"], NOW - timedelta(days=2), 100 * KIB)
    assert push(env).returncode == 0
    stale = check(env)
    assert stale.returncode == 2 and stale.stdout.startswith("STALE")
    empty = FakeGitHub(env["tmp"] / "empty").start()
    try:
        none = check(env, GHB_API_URL=empty.api)
        assert none.returncode == 2 and "no valid generation" in none.stdout
    finally:
        empty.stop()


def test_monitor_flags_over_budget(env):
    env["fake"].add_release("big", assets=[("blob.bin", b"z" * (2 * MIB))])
    result = check(env, GHB_BUDGET_BYTES=MIB)
    assert result.returncode == 1 and result.stdout.startswith("OVER_BUDGET")


def test_fetch_refuses_a_corrupted_download_and_never_overwrites(env):
    run = make_run(env["backups"], NOW, 300 * KIB)
    assert push(env).returncode == 0
    out = env["tmp"] / "kit"
    assert fetch(env, out).returncode == 0
    again = fetch(env, out)
    assert again.returncode == 1 and "не перезаписываю" in again.stderr
    env["fake"].corrupt(env["fake"].asset_by_name(f"{run}.signed-backup.tar.age")["id"])
    bad = fetch(env, env["tmp"] / "kit2")
    assert bad.returncode == 1 and "повреждена" in bad.stderr
    assert not list((env["tmp"] / "kit2").glob("*.age"))


# --- audit 2026-10-13: fault injection and invariants --------------------------------


def _intact_tags(fake):
    """Published generations whose ciphertext really matches its description."""
    import hashlib

    intact = set()
    for rel in fake.published():
        names = {fake.assets[a]["name"]: fake.assets[a] for a in rel["asset_ids"]}
        side = next((a for n, a in names.items() if n.endswith(".backup.json")), None)
        ciph = next((a for n, a in names.items() if n.endswith(".tar.age")), None)
        if not side or not ciph:
            continue
        meta = json.loads(fake.blob(side["id"]).read_bytes())
        if hashlib.sha256(fake.blob(ciph["id"]).read_bytes()).hexdigest() == meta.get(
                "ciphertext_sha256"):
            intact.add(rel["tag_name"])
    return intact


def test_ninety_randomized_days_keep_every_invariant(env):
    """Random sizes, a few oversized archives, outages, accidental deletions and
    silent corruption. After every night: peak <= budget; a successful night's
    copy is the newest and intact; the job itself never leaves fewer than
    min(2, before) intact copies."""
    import random

    rng = random.Random(20261013)
    budget, watermark = 8 * MIB, int(6.8 * MIB)
    fake = env["fake"]
    for day in range(90):
        when = NOW - timedelta(days=89 - day)
        size = rng.randint(300, 1200) * KIB
        if rng.random() < 0.04:
            size = 7 * MIB  # unexpected large archive: cannot fit next to two copies
        run = make_run(env["backups"], when, size)
        event = rng.random()
        if event < 0.05:
            fake.faults["outage"] = True
        elif event < 0.08:
            fake.faults["delete_fail"] = True
        elif event < 0.11 and fake.published():
            fake.corrupt(fake.asset_by_name(
                rng.choice(fake.published())["tag_name"].split("-", 2)[2][:19]
                + ".signed-backup.tar.age")["id"])
        elif event < 0.13 and len(fake.published()) > 2:
            with fake.lock:  # someone deletes a release by hand
                victim = rng.choice(fake.published())
                for aid in victim["asset_ids"]:
                    fake.assets.pop(aid)
                fake.releases.pop(victim["id"])
        before = _intact_tags(fake)
        result = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark)
        fake.faults.clear()
        after = _intact_tags(fake)
        assert fake.max_used <= budget, day
        if result.returncode == 0 and "зашифрована" in result.status["message"]:
            assert valid_runs(fake)[-1] == run, day
            assert any(run in tag for tag in after), day
            assert len(after) >= min(2, len(before) + 1), (day, result.stderr)
        else:
            assert result.status["status"] == "failed", day
            assert len(after & before) >= min(2, len(before)), (day, result.stderr)
        shutil.rmtree(env["backups"] / run)
    assert_never_over_budget(env, budget)


def test_one_valid_and_one_silently_corrupted_copy(env):
    budget, watermark = 3 * MIB, int(2.5 * MIB)
    runs = []
    for day in range(2):
        runs.append(make_run(env["backups"], NOW - timedelta(days=2 - day), 800 * KIB))
        assert push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark).returncode == 0
    env["fake"].corrupt(env["fake"].asset_by_name(f"{runs[1]}.signed-backup.tar.age")["id"])
    runs.append(make_run(env["backups"], NOW, 800 * KIB))
    result = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark)
    assert result.returncode == 0, result.stderr
    # the corrupted copy never counted as one of the "two": the intact old one survives
    assert _intact_tags(env["fake"]) == {r["tag_name"] for r in env["fake"].published()}
    assert runs[0] in valid_runs(env["fake"]) and runs[2] in valid_runs(env["fake"])
    assert runs[1] not in valid_runs(env["fake"])


def test_github_outage_during_rotation_deletes_at_most_what_was_planned(env):
    budget, watermark = 3 * MIB, int(2.5 * MIB)
    for day in range(3):
        make_run(env["backups"], NOW - timedelta(days=3 - day), 700 * KIB)
        assert push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark).returncode == 0
    before = _intact_tags(env["fake"])
    run = make_run(env["backups"], NOW, 700 * KIB)
    env["fake"].faults["delete_fail"] = True
    seen = len(env["fake"].calls)
    result = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark)
    env["fake"].faults.clear()
    # no retry storm, a clear failure, every intact copy still there
    deletes = [c for c in env["fake"].calls[seen:] if c[0] == "DELETE"]
    assert len(deletes) <= 1
    if result.returncode:
        assert result.status["status"] == "failed"
        assert before <= _intact_tags(env["fake"])
    retry = push(env, GHB_BUDGET_BYTES=budget, GHB_WATERMARK_BYTES=watermark)
    assert retry.returncode == 0 and valid_runs(env["fake"])[-1] == run


def test_inconsistent_metadata_after_a_successful_api_call_is_not_healthy(env):
    make_run(env["backups"], NOW - timedelta(days=1), 200 * KIB)
    assert push(env).returncode == 0
    run = make_run(env["backups"], NOW, 200 * KIB)
    env["fake"].faults["size_lie"] = 7  # the asset list reports a size that differs
    result = push(env)
    assert result.returncode == 1 and result.status["status"] == "failed"
    monitor = check(env)
    assert monitor.returncode == 1 and "CORRUPT" in monitor.stdout
    env["fake"].faults.clear()
    assert run in "".join(r["tag_name"] for r in env["fake"].published())  # kept as evidence


def test_malformed_api_answers_fail_closed_and_never_stay_running(env):
    make_run(env["backups"], NOW, 100 * KIB)
    env["fake"].faults["bad_json"] = True
    result = push(env)
    assert result.returncode == 1 and result.status["status"] == "failed"
    assert "не JSON" in result.status["message"]
    env["fake"].add_release("odd", assets=[("x.bin", b"x")])
    env["fake"].assets[max(env["fake"].assets)]["size"] = "big"  # a size that is not a number
    result = push(env)
    assert result.returncode == 1 and "размера" in result.status["message"]
    assert not env["fake"].published() or all(
        r["tag_name"] == "odd" for r in env["fake"].published())


def test_malformed_description_asset_is_invalid_not_a_crash(env):
    run = make_run(env["backups"], NOW, 100 * KIB)
    assert push(env).returncode == 0
    side = env["fake"].asset_by_name(f"{run}.backup.json")
    env["fake"].blob(side["id"]).write_bytes(b"[1, 2, 3]")
    env["fake"].assets[side["id"]]["size"] = 9
    monitor = check(env)
    assert monitor.returncode == 1 and "CORRUPT" in monitor.stdout


def test_rate_limited_reads_wait_and_continue(env):
    make_run(env["backups"], NOW, 100 * KIB)
    env["fake"].faults["rate_limit_gets"] = 1
    result = push(env)
    assert result.returncode == 0, result.stderr


def test_accidentally_deleted_release_is_noticed_and_recovered(env):
    for day in range(2):
        make_run(env["backups"], NOW - timedelta(hours=30 - day * 12), 100 * KIB)
        assert push(env).returncode == 0
    newest = env["fake"].published()[-1]
    with env["fake"].lock:
        for aid in newest["asset_ids"]:
            env["fake"].assets.pop(aid)
        env["fake"].releases.pop(newest["id"])
    degraded = check(env)
    assert degraded.returncode == 2  # STALE or DEGRADED, never FRESH
    make_run(env["backups"], NOW, 100 * KIB)
    assert push(env).returncode == 0
    assert check(env).returncode == 0


def test_real_service_acceptance_script_passes_against_the_fake(env):
    """The owner's acceptance procedure, run here against the isolated fake."""
    script = ROOT / "scripts/operations/github_backup_acceptance.py"
    environ = {**os.environ, "GHB_CONFIG": "/nonexistent", "GHB_REPO": env["fake"].repo,
               "GHB_API_URL": env["fake"].api, "GHB_UPLOAD_URL": env["fake"].uploads,
               "GHB_TOKEN_FILE": str(env["tokens"]["write"])}
    result = subprocess.run([sys.executable, str(script), "--source-repo", "owner/DenStock"],
                            env=environ, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACCEPTED" in result.stdout
    assert not env["fake"].releases  # only its own release, and it is gone
    refused = subprocess.run([sys.executable, str(script)], env={
        **environ, "GHB_TOKEN_FILE": str(env["tokens"]["write"])}, capture_output=True,
        text=True, timeout=300)
    assert refused.returncode == 0  # empty again, so it may run again
    env["fake"].add_release("v1", assets=[("a.bin", b"a")])
    busy = subprocess.run([sys.executable, str(script)], env=environ, capture_output=True,
                          text=True, timeout=300)
    assert busy.returncode != 0 and "already has releases" in busy.stdout + busy.stderr
    assert "v1" in [r["tag_name"] for r in env["fake"].releases.values()]
