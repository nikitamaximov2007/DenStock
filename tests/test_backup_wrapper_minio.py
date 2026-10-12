"""The offsite wrapper against a REAL S3 protocol: rclone + MinIO, versioning on.

The synthetic harness emulates S3 in Python; these tests replace its fake
rclone with the real binary talking to a local MinIO bucket with versioning
enabled (like the production Yandex bucket). backup_all/verify_backup stay
synthetic. Skipped when rclone or minio is not installed. Nothing here can
reach a real cloud: the rclone config is generated per test and points only at
127.0.0.1.
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import time

import pytest

from tests.offsite_harness import OffsiteHarness, _signed_run

pytestmark = pytest.mark.skipif(
    not (shutil.which("rclone") and shutil.which("minio")),
    reason="rclone and minio are required for the real-S3 wrapper tests",
)

OLDEST = "2026-10-07_03-00-00"
PREVIOUS = "2026-10-09_03-00-00"
NEW = "2026-10-11_03-00-12"
LATER = "2026-10-12_03-00-07"
LAST = "2026-10-13_03-00-05"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def minio(tmp_path):
    port, console = _free_port(), _free_port()
    user, password = "dr" + secrets.token_hex(4), secrets.token_urlsafe(24)
    env = {**os.environ, "MINIO_ROOT_USER": user, "MINIO_ROOT_PASSWORD": password,
           "MINIO_BROWSER": "off"}
    data = tmp_path / "minio-data"
    data.mkdir()
    server = subprocess.Popen(
        ["minio", "server", str(data), "--address", f"127.0.0.1:{port}",
         "--console-address", f"127.0.0.1:{console}", "--quiet"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[s3test]\ntype = s3\nprovider = Minio\n"
        f"access_key_id = {user}\nsecret_access_key = {password}\n"
        f"endpoint = http://127.0.0.1:{port}\nregion = us-east-1\n"
    )
    conf.chmod(0o600)
    rclone = ["rclone", "--config", str(conf)]
    deadline = time.time() + 30
    while time.time() < deadline:
        if subprocess.run([*rclone, "lsd", "s3test:"], capture_output=True).returncode == 0:
            break
        time.sleep(0.5)
    else:
        server.kill()
        pytest.fail("minio did not start")
    subprocess.run([*rclone, "mkdir", "s3test:bucket"], check=True, capture_output=True)
    subprocess.run([*rclone, "backend", "versioning", "s3test:bucket", "Enabled"],
                   check=True, capture_output=True)
    try:
        yield {"rclone": rclone, "conf": conf, "target": "s3test:bucket/denstock"}
    finally:
        server.terminate()
        server.wait(timeout=10)


def _real_harness(tmp_path, minio, soft_limit):
    harness = OffsiteHarness(tmp_path, soft_limit=soft_limit)
    (harness.bin / "rclone").unlink()  # the real rclone from PATH is used instead
    (harness.root / ".env.backup").write_text(
        "BACKUP_OFFSITE_ENABLED=true\n"
        f"BACKUP_OFFSITE_TARGET={minio['target']}\n"
        f"BACKUP_REMOTE_SOFT_LIMIT_BYTES={soft_limit}\n"
    )
    return harness


def _run(harness, minio, run_id):
    return harness.run(run_id, RCLONE_CONFIG=str(minio["conf"]))


def _seed(harness, minio, run_id):
    local = harness.root / "backups" / run_id
    _signed_run(local, run_id, harness.run_bytes)
    subprocess.run([*minio["rclone"], "copy", str(local), f"{minio['target']}/{run_id}"],
                   check=True, capture_output=True)


def _remote_runs(minio):
    out = subprocess.run([*minio["rclone"], "lsf", minio["target"], "--dirs-only"],
                         capture_output=True, text=True, check=True).stdout
    return sorted(line.rstrip("/") for line in out.splitlines() if line.strip())


def _versioned_bytes(minio):
    out = subprocess.run([*minio["rclone"], "size", "--s3-versions", minio["target"], "--json"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["bytes"]


def _gen_bytes(tmp_path):
    probe = tmp_path / "probe" / NEW
    _signed_run(probe, NEW, 40)
    return sum(p.stat().st_size for p in probe.rglob("*") if p.is_file())


def test_upload_reads_back_byte_for_byte_from_real_s3(tmp_path, minio):
    harness = _real_harness(tmp_path, minio, soft_limit=10 * _gen_bytes(tmp_path))
    _seed(harness, minio, PREVIOUS)

    result = _run(harness, minio, NEW)

    assert result.returncode == 0, result.stderr
    assert result.status["status"] == "ok"
    assert _remote_runs(minio) == [PREVIOUS, NEW]
    check = subprocess.run(
        [*minio["rclone"], "check", "--download", str(harness.root / "backups" / NEW),
         f"{minio['target']}/{NEW}"], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_rotation_on_a_versioned_bucket_really_frees_space(tmp_path, minio):
    generation = _gen_bytes(tmp_path)
    harness = _real_harness(tmp_path, minio, soft_limit=3 * generation + 10)
    for run_id in (OLDEST, PREVIOUS):
        _seed(harness, minio, run_id)
    before = _versioned_bytes(minio)

    result = _run(harness, minio, NEW)

    assert result.returncode == 0, result.stderr
    assert _remote_runs(minio) == [PREVIOUS, NEW]
    # purge alone leaves noncurrent versions; the targeted cleanup-hidden must
    # remove the purged generation's versions, so versioned bytes really drop.
    after = _versioned_bytes(minio)
    assert after < before + generation
    plain = json.loads(subprocess.run(
        [*minio["rclone"], "size", minio["target"], "--json"],
        capture_output=True, text=True, check=True).stdout)["bytes"]
    assert after == plain  # no hidden versions of the purged generation remain


def test_interrupted_upload_left_on_real_s3_is_removed_next_night(tmp_path, minio):
    harness = _real_harness(tmp_path, minio, soft_limit=10 * _gen_bytes(tmp_path))
    _seed(harness, minio, PREVIOUS)
    _seed(harness, minio, LATER)
    # Truncate one object of LATER on the remote: a cut upload.
    dump = harness.root / "backups" / LATER / "db.dump"
    cut = tmp_path / "cut-db.dump"
    cut.write_bytes(dump.read_bytes()[: max(1, dump.stat().st_size // 3)])
    subprocess.run([*minio["rclone"], "copyto", str(cut), f"{minio['target']}/{LATER}/db.dump"],
                   check=True, capture_output=True)

    result = _run(harness, minio, LAST)

    assert result.returncode == 0, result.stderr
    assert result.status["status"] == "ok"
    assert _remote_runs(minio) == [PREVIOUS, LAST]


def test_silently_corrupted_remote_copy_is_caught_by_real_read_back(tmp_path, minio):
    harness = _real_harness(tmp_path, minio, soft_limit=10 * _gen_bytes(tmp_path))
    _seed(harness, minio, PREVIOUS)
    assert _run(harness, minio, NEW).returncode == 0
    # Same size, different bytes: only a content comparison can see it.
    dump = harness.root / "backups" / NEW / "db.dump"
    flipped = tmp_path / "flipped.dump"
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    flipped.write_bytes(bytes(data))
    subprocess.run([*minio["rclone"], "copyto", str(flipped), f"{minio['target']}/{NEW}/db.dump"],
                   check=True, capture_output=True)
    hash_check = subprocess.run(
        [*minio["rclone"], "check", str(dump.parent), f"{minio['target']}/{NEW}"],
        capture_output=True, text=True)
    assert hash_check.returncode != 0  # ETag/MD5 comparison sees it too

    result = _run(harness, minio, LAST)

    # The corrupted NEW is newer than the proven PREVIOUS: it is not a recovery
    # point, so it is removed and the night still ends with two proven copies.
    assert result.returncode == 0, result.stderr
    assert _remote_runs(minio) == [PREVIOUS, LAST]


def test_repeated_run_id_is_never_overwritten_on_real_s3(tmp_path, minio):
    harness = _real_harness(tmp_path, minio, soft_limit=10 * _gen_bytes(tmp_path))
    _seed(harness, minio, NEW)
    shutil.rmtree(harness.root / "backups" / NEW)  # only the remote copy exists
    remote_before = subprocess.run(
        [*minio["rclone"], "lsjson", "-R", f"{minio['target']}/{NEW}"],
        capture_output=True, text=True, check=True).stdout
    result = _run(harness, minio, NEW)
    assert result.returncode == 1
    assert "перезапись запрещена" in result.stderr
    remote_after = subprocess.run(
        [*minio["rclone"], "lsjson", "-R", f"{minio['target']}/{NEW}"],
        capture_output=True, text=True, check=True).stdout
    assert remote_after == remote_before
