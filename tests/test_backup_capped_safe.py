"""Synthetic host-wrapper tests: no cloud credentials or customer payloads."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "scripts/operations/denstock-backup-capped-safe.sh"
)
NEW_RUN = "2026-10-11_03-00-12"


@pytest.fixture
def fake_host(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "backups").mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    logfile = tmp_path / "calls.jsonl"
    docker = binaries / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import os, pathlib, sys, time
root = pathlib.Path(os.environ['FAKE_PROJECT'])
args = sys.argv[1:]
if 'backup_all' in args:
    run = root / 'backups' / '2026-10-11_03-00-12'
    run.mkdir()
    (run / 'manifest.json').write_bytes(b'signed synthetic manifest')
    (run / 'db.dump').write_bytes(b'd' * 40)
    if os.environ.get('FAKE_BACKUP_SLEEP'):
        (root / 'backup-in-progress').write_text('ready')
        time.sleep(float(os.environ['FAKE_BACKUP_SLEEP']))
    print(f'Полный бэкап готов: {run}')
elif 'verify_backup' in args:
    run = root / 'backups' / args[-1]
    if not (run / 'manifest.json').is_file():
        sys.exit(2)
else:
    sys.exit(2)
"""
    )
    docker.chmod(0o755)
    flock = binaries / "flock"
    flock.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, sys\n"
        "try:\n"
        "    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    sys.exit(1)\n"
    )
    flock.chmod(0o755)
    rclone = binaries / "rclone"
    rclone.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
remote = pathlib.Path(os.environ['FAKE_REMOTE'])
args = sys.argv[1:]
with open(os.environ['FAKE_CALLS'], 'a') as out:
    out.write(json.dumps(args) + '\\n')
def path(value):
    if not value.startswith('fake:bucket'):
        sys.exit(9)
    suffix = value[len('fake:bucket'):].lstrip('/')
    return remote / suffix
if args[0] == 'size':
    files = [p for p in path(args[2]).rglob('*') if p.is_file()]
    print(json.dumps({'bytes': sum(p.stat().st_size for p in files), 'count': len(files)}))
elif args[0] == 'lsf':
    print(''.join(p.name + '/\\n' for p in path(args[1]).iterdir() if p.is_dir()))
elif args[0] == 'copy':
    if os.environ.get('FAKE_UPLOAD_FAIL') == '1':
        sys.exit(10)
    if os.environ.get('FAKE_PARTIAL_UPLOAD_FAIL') == '1':
        path(args[2]).mkdir()
        (path(args[2]) / 'db.dump').write_bytes(b'incomplete')
        sys.exit(10)
    shutil.copytree(args[1], path(args[2]))
elif args[0] == 'check':
    if os.environ.get('FAKE_CHECK_FAIL') == '1':
        sys.exit(11)
    source = pathlib.Path(args[2])
    target = path(args[3])
    left = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    right = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    if left != right:
        sys.exit(12)
elif args[0] == 'purge':
    shutil.rmtree(path(args[1]))
elif args[:2] == ['backend', 'cleanup-hidden']:
    if os.environ.get('FAKE_ACCOUNTING_DELAY') == '1':
        # A provider whose size accounting does not shrink after deletion.
        (remote / 'hidden-version').write_bytes(b'x' * 100)
else:
    sys.exit(13)
"""
    )
    rclone.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "DENSTOCK_PROJECT_ROOT": str(root),
        "DENSTOCK_BACKUP_LOCK_FILE": str(tmp_path / "backup.lock"),
        "FAKE_PROJECT": str(root),
        "FAKE_REMOTE": str(remote),
        "FAKE_CALLS": str(logfile),
    }
    return {"root": root, "remote": remote, "calls": logfile, "env": env}


def _configure(host, limit):
    (host["root"] / ".env.backup").write_text(
        "BACKUP_OFFSITE_ENABLED=true\n"
        "BACKUP_OFFSITE_TARGET=fake:bucket\n"
        f"BACKUP_REMOTE_SOFT_LIMIT_BYTES={limit}\n"
    )


def _old(host, run_id, *, bytes_count=60, local=True):
    target = host["remote"] / run_id
    target.mkdir()
    (target / "manifest.json").write_bytes(b"signed synthetic manifest")
    (target / "db.dump").write_bytes(b"d" * bytes_count)
    if local:
        shutil.copytree(target, host["root"] / "backups" / run_id)
    return target


def _execute(host, **overrides):
    result = subprocess.run(
        ["bash", str(WRAPPER)], cwd=host["root"],
        env={**host["env"], **overrides}, capture_output=True, text=True,
    )
    if result.returncode and not (host["root"] / "backups" / NEW_RUN).exists():
        raise AssertionError(result.stderr or result.stdout)
    return result


def _calls(host):
    if not host["calls"].exists():
        return []
    return [json.loads(line) for line in host["calls"].read_text().splitlines()]


def _status(host):
    return json.loads((host["root"] / "backups/offsite_status.json").read_text())


def test_upload_failure_preserves_all_previous_remote_generations(fake_host):
    old = _old(fake_host, "2026-10-09_03-00-00")
    _configure(fake_host, 1000)

    result = _execute(fake_host, FAKE_UPLOAD_FAIL="1")

    assert result.returncode != 0
    assert old.is_dir()
    assert (fake_host["root"] / "backups" / NEW_RUN / "db.dump").is_file()
    assert not any(call[0] == "purge" for call in _calls(fake_host))
    assert _status(fake_host)["status"] == "failed"


def test_insufficient_space_refuses_upload_without_predelete(fake_host):
    old = _old(fake_host, "2026-10-09_03-00-00")
    _configure(fake_host, 110)

    result = _execute(fake_host)

    assert result.returncode != 0
    assert old.is_dir()
    assert not any(call[0] in {"copy", "purge"} for call in _calls(fake_host))
    assert _status(fake_host)["status"] == "failed"


def test_unrecognized_remote_directory_blocks_upload_and_rotation(fake_host):
    old = _old(fake_host, "2026-10-09_03-00-00")
    (fake_host["remote"] / "unrecognized").mkdir()
    _configure(fake_host, 1000)

    result = _execute(fake_host)

    assert result.returncode != 0
    assert old.is_dir()
    assert not any(call[0] in {"copy", "purge"} for call in _calls(fake_host))


def test_failed_readback_never_starts_retention(fake_host):
    old = _old(fake_host, "2026-10-09_03-00-00")
    _configure(fake_host, 1000)

    result = _execute(fake_host, FAKE_CHECK_FAIL="1")

    assert result.returncode != 0
    assert old.is_dir()
    assert not any(call[0] == "purge" for call in _calls(fake_host))
    assert _status(fake_host)["status"] == "failed"


def test_partial_upload_is_preserved_for_investigation_and_never_rotates(fake_host):
    old = _old(fake_host, "2026-10-09_03-00-00")
    _configure(fake_host, 1000)

    first = _execute(fake_host, FAKE_PARTIAL_UPLOAD_FAIL="1")
    second = _execute(fake_host)

    assert first.returncode != 0
    assert second.returncode != 0  # remote run ID collision, no overwrite
    assert old.is_dir()
    assert (fake_host["remote"] / NEW_RUN / "db.dump").read_bytes() == b"incomplete"
    assert not any(call[0] == "purge" for call in _calls(fake_host))


def test_retention_only_purges_after_readback_and_preserves_two_generations(fake_host):
    oldest = _old(fake_host, "2026-10-07_03-00-00")
    previous = _old(fake_host, "2026-10-09_03-00-00")
    _configure(fake_host, 270)

    result = _execute(fake_host)

    assert result.returncode == 0, result.stderr
    assert not oldest.exists()
    assert previous.is_dir()
    assert (fake_host["remote"] / NEW_RUN).is_dir()
    calls = _calls(fake_host)
    assert any(i < next(j for j, item in enumerate(calls) if item[0] == "purge")
               for i, call in enumerate(calls) if call[0] == "check")
    assert [call[0] for call in calls[-2:]] == ["check", "check"]
    assert all(call[1].endswith("/2026-10-07_03-00-00") for call in calls
               if call[0] == "purge")
    assert _status(fake_host)["status"] == "ok"


def test_missing_previous_local_evidence_blocks_rotation(fake_host):
    oldest = _old(fake_host, "2026-10-07_03-00-00")
    _old(fake_host, "2026-10-09_03-00-00", local=False)
    _configure(fake_host, 270)

    result = _execute(fake_host)

    assert result.returncode != 0
    assert oldest.is_dir()
    assert not any(call[0] == "purge" for call in _calls(fake_host))
    assert _status(fake_host)["status"] == "degraded"


def test_accounting_delay_stops_further_retention(fake_host):
    _old(fake_host, "2026-10-07_03-00-00")
    previous = _old(fake_host, "2026-10-09_03-00-00")
    _configure(fake_host, 270)

    result = _execute(fake_host, FAKE_ACCOUNTING_DELAY="1")

    assert result.returncode != 0
    assert previous.is_dir()
    assert (fake_host["remote"] / NEW_RUN).is_dir()
    assert _status(fake_host)["status"] == "failed"


def test_two_concurrent_publishers_do_not_start_a_second_backup(fake_host):
    _configure(fake_host, 1000)
    with subprocess.Popen(
        ["bash", str(WRAPPER)], cwd=fake_host["root"],
        env={**fake_host["env"], "FAKE_BACKUP_SLEEP": "2"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as first:
        marker = fake_host["root"] / "backup-in-progress"
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "first backup never reached the lock-protected stage"
        second = subprocess.run(
            ["bash", str(WRAPPER)], cwd=fake_host["root"],
            env=fake_host["env"], capture_output=True, text=True,
        )
        _stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, stderr

    assert second.returncode == 75
    assert "backup already running" in second.stderr
    assert len([call for call in _calls(fake_host) if call[0] == "copy"]) == 1
