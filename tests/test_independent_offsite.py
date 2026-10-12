"""Independent encrypted offsite copy against real S3 (rclone + MinIO).

The push job runs with credentials that may list, read and write but NOT
delete (a MinIO policy created per test). Everything is synthetic; the age
recipient belongs to a throwaway test identity.
"""

import json
import os
import secrets
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.offsite_harness import FLOCK
from tests.test_backup_wrapper_minio import _free_port

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("rclone", "minio", "mc", "age", "age-keygen")),
    reason="rclone, minio, mc and age are required",
)

ROOT = Path(__file__).resolve().parents[1]
PUSH = ROOT / "scripts/operations/independent_offsite_push.sh"
CHECK = ROOT / "scripts/operations/independent_offsite_check.sh"
MARKER = b"PLAINTEXT-CUSTOMER-MARKER-7f3a"


@pytest.fixture
def indep(tmp_path):
    import time

    port, console = _free_port(), _free_port()
    admin, admin_pw = "root" + secrets.token_hex(3), secrets.token_urlsafe(24)
    data = tmp_path / "minio-data"
    data.mkdir()
    server = subprocess.Popen(
        ["minio", "server", str(data), "--address", f"127.0.0.1:{port}",
         "--console-address", f"127.0.0.1:{console}", "--quiet"],
        env={**os.environ, "MINIO_ROOT_USER": admin, "MINIO_ROOT_PASSWORD": admin_pw,
             "MINIO_BROWSER": "off"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    mc_home = tmp_path / "mc"
    mc = ["mc", "--config-dir", str(mc_home)]
    deadline = time.time() + 30
    while subprocess.run([*mc, "alias", "set", "t", f"http://127.0.0.1:{port}", admin, admin_pw],
                         capture_output=True).returncode != 0:
        if time.time() > deadline:
            server.kill()
            pytest.fail("minio did not start")
        time.sleep(0.5)
    run = lambda *args: subprocess.run([*mc, *args], check=True, capture_output=True)  # noqa: E731
    run("mb", "t/indep")
    run("version", "enable", "t/indep")
    policy = tmp_path / "append-only.json"
    policy.write_text(json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": ["arn:aws:s3:::indep"]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": ["arn:aws:s3:::indep/*"]},
    ]}))
    pusher, pusher_pw = "push" + secrets.token_hex(3), secrets.token_urlsafe(24)
    run("admin", "policy", "create", "t", "append-only", str(policy))
    run("admin", "user", "add", "t", pusher, pusher_pw)
    run("admin", "policy", "attach", "t", "append-only", "--user", pusher)

    def conf(name, user, pw):
        path = tmp_path / f"{name}.conf"
        # no_check_bucket: list/get/put-only keys cannot CreateBucket (403).
        path.write_text("[indep]\ntype = s3\nprovider = Minio\nno_check_bucket = true\n"
                        f"access_key_id = {user}\nsecret_access_key = {pw}\n"
                        f"endpoint = http://127.0.0.1:{port}\nregion = us-east-1\n")
        path.chmod(0o600)
        return path

    identity = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = tmp_path / "recipient.txt"
    recipient.write_text(subprocess.run(["age-keygen", "-y", str(identity)], check=True,
                                        capture_output=True, text=True).stdout)
    backups = tmp_path / "backups"
    backups.mkdir()
    shim = tmp_path / "shim"
    shim.mkdir()
    if not shutil.which("flock"):  # macOS has no flock; Ubuntu does
        (shim / "flock").write_text(FLOCK)
        (shim / "flock").chmod(0o755)
    try:
        yield {"push_conf": conf("pusher", pusher, pusher_pw),
               "admin_conf": conf("admin", admin, admin_pw), "identity": identity,
               "recipient": recipient, "backups": backups, "tmp": tmp_path, "data": data,
               "shim": shim}
    finally:
        server.terminate()
        server.wait(timeout=10)


def _run_dir(backups, run_id, *, verified=True, hours_ago=1):
    run = backups / run_id
    run.mkdir()
    (run / "db.dump").write_bytes(MARKER + os.urandom(20000))
    (run / "private_media.tar.gz").write_bytes(MARKER + b" private " + os.urandom(5000))
    created = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    (run / "manifest.json").write_text(json.dumps({
        "verification_status": "verified" if verified else "failed",
        "created_at": created, "app_commit": "f" * 40, "private_media_status": "included"}))
    return run


def _push(indep, **env):
    return subprocess.run(
        ["bash", str(PUSH)], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PATH": f"{indep['shim']}:{os.environ['PATH']}",
             "INDEP_CONFIG": "/nonexistent",
             "INDEP_TARGET": "indep:indep/denstock", "INDEP_RCLONE_CONFIG": str(indep["push_conf"]),
             "INDEP_RECIPIENT_FILE": str(indep["recipient"]), "BACKUP_ROOT": str(indep["backups"]),
             "INDEP_LOCK_FILE": str(indep["tmp"] / "push.lock"), **env})


def _status(indep):
    return json.loads((indep["backups"] / "independent_status.json").read_text())


def _rclone(indep, conf, *args):
    return subprocess.run(["rclone", "--config", str(conf), *args], capture_output=True, text=True)


def _check(indep, *extra):
    return subprocess.run(
        ["bash", str(CHECK), "--target", "indep:indep/denstock",
         "--config", str(indep["push_conf"]), *extra], capture_output=True, text=True)


def test_push_encrypts_reads_back_and_decrypts_to_the_same_bytes(indep):
    run = _run_dir(indep["backups"], "2026-10-12_03-00-01")
    result = _push(indep)
    assert result.returncode == 0, result.stderr
    assert _status(indep)["status"] == "ok"
    listing = _rclone(indep, indep["push_conf"], "lsf", "indep:indep/denstock").stdout.split()
    assert listing == ["2026-10-12_03-00-01.signed-backup.tar.age",
                       "2026-10-12_03-00-01.signed-backup.tar.age.json"]
    restored = indep["tmp"] / "restored"
    restored.mkdir()
    cat = subprocess.Popen(["rclone", "--config", str(indep["push_conf"]), "cat",
                            "indep:indep/denstock/2026-10-12_03-00-01.signed-backup.tar.age"],
                           stdout=subprocess.PIPE)
    dec = subprocess.Popen(["age", "-d", "-i", str(indep["identity"])], stdin=cat.stdout,
                           stdout=subprocess.PIPE)
    subprocess.run(["tar", "-xf", "-", "-C", str(restored)], stdin=dec.stdout, check=True)
    for name in ("db.dump", "private_media.tar.gz", "manifest.json"):
        assert (restored / run.name / name).read_bytes() == (run / name).read_bytes()


def test_plaintext_never_reaches_the_provider(indep):
    _run_dir(indep["backups"], "2026-10-12_03-00-01")
    assert _push(indep).returncode == 0
    for path in indep["data"].rglob("*"):
        if path.is_file():
            assert MARKER not in path.read_bytes(), path


def test_second_push_of_the_same_run_changes_nothing(indep):
    _run_dir(indep["backups"], "2026-10-12_03-00-01")
    assert _push(indep).returncode == 0
    def objects():  # files only: MinIO synthesizes directory mtimes per request
        return _rclone(indep, indep["admin_conf"], "lsjson", "-R", "--files-only", "--hash",
                       "indep:indep").stdout
    before = objects()
    second = _push(indep)
    assert second.returncode == 0
    assert "уже выгружена" in _status(indep)["message"]
    assert objects() == before


def test_push_credentials_cannot_delete_the_independent_copy(indep):
    _run_dir(indep["backups"], "2026-10-12_03-00-01")
    assert _push(indep).returncode == 0
    target = "indep:indep/denstock/2026-10-12_03-00-01.signed-backup.tar.age"
    deleted = _rclone(indep, indep["push_conf"], "deletefile", target)
    assert deleted.returncode != 0
    assert _rclone(indep, indep["push_conf"], "lsf", target).stdout.strip()


def test_only_a_verified_run_is_pushed(indep):
    _run_dir(indep["backups"], "2026-10-12_03-00-01")
    _run_dir(indep["backups"], "2026-10-13_03-00-01", verified=False)
    assert _push(indep).returncode == 0
    assert _status(indep)["run"] == "2026-10-12_03-00-01"


def test_cut_upload_never_counts_and_is_never_overwritten(indep, tmp_path):
    _run_dir(indep["backups"], "2026-10-12_03-00-01")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    real = shutil.which("rclone")
    (fake / "rclone").write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do if [[ "$a" == rcat ]]; then head -c 100 >/dev/null; exit 1; fi; done\n'
        f'exec "{real}" "$@"\n')
    (fake / "rclone").chmod(0o755)
    cut = _push(indep, PATH=f"{fake}:{indep['shim']}:{os.environ['PATH']}")
    assert cut.returncode == 1
    assert _status(indep)["status"] == "failed"
    assert _check(indep).returncode == 2  # no confirmed copy
    assert _push(indep).returncode == 0
    assert _check(indep, "--deep").returncode == 0


def test_freshness_check_flags_stale_and_corrupt_copies(indep):
    _run_dir(indep["backups"], "2026-10-10_03-00-01", hours_ago=40)
    assert _push(indep).returncode == 0
    stale = _check(indep)
    assert stale.returncode == 2 and stale.stdout.startswith("STALE")
    target = "indep:indep/denstock/2026-10-10_03-00-01.signed-backup.tar.age"
    junk = indep["tmp"] / "junk"
    junk.write_bytes(b"not the original ciphertext")
    assert _rclone(indep, indep["admin_conf"], "copyto", str(junk), target).returncode == 0
    corrupt = _check(indep, "--deep", "--max-age-hours", "1000")
    assert corrupt.returncode == 1 and "CORRUPT" in corrupt.stdout


def test_failed_push_exits_non_zero_so_alerting_sees_it(indep, tmp_path):
    """Regression: the cleanup trap used to return rm's status (0) after a failure."""
    _run_dir(indep["backups"], "2026-10-12_03-00-01")
    broken = tmp_path / "broken.conf"
    broken.write_text("[indep]\ntype = s3\nprovider = Minio\nendpoint = http://127.0.0.1:9\n"
                      "access_key_id = x\nsecret_access_key = y\n")
    fast = "--contimeout 2s --timeout 5s --retries 1 --low-level-retries 1"
    result = _push(indep, INDEP_RCLONE_CONFIG=str(broken), INDEP_RCLONE_FLAGS=fast)
    assert result.returncode != 0
    assert _status(indep)["status"] == "failed"
