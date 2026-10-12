import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/operations/backup_offsite.sh"


def run_wrapper(tmp_path, *, failure=None):
    project = tmp_path / "project"
    operations = project / "scripts/operations"
    operations.mkdir(parents=True)
    (project / "backups").mkdir()
    shutil.copy2(SCRIPT, operations / SCRIPT.name)
    (project / ".env.backup").write_text(
        "BACKUP_OFFSITE_ENABLED=true\n"
        "BACKUP_OFFSITE_METHOD=rclone\n"
        "BACKUP_OFFSITE_TARGET=remote:bucket\n"
        "BACKUP_KEEP_LAST=14\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    events = tmp_path / "events.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f"echo docker:$* >> '{events}'\n"
        "if [[ \"$*\" == *backup_all* ]]; then\n"
        "  mkdir -p backups/2026-10-12_03-00-00\n"
        "  echo verified > backups/2026-10-12_03-00-00/manifest.json\n"
        "fi\n",
        encoding="utf-8",
    )
    rclone = bin_dir / "rclone"
    rclone.write_text(
        "#!/bin/bash\n"
        f"echo rclone:$* >> '{events}'\n"
        "if [[ \"$1\" == \"${RCLONE_FAIL_COMMAND:-}\" ]]; then exit 9; fi\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    rclone.chmod(0o755)
    flock = bin_dir / "flock"
    flock.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    flock.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["RCLONE_FAIL_COMMAND"] = failure or ""
    result = subprocess.run(
        ["bash", str(operations / SCRIPT.name)],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    lines = events.read_text(encoding="utf-8").splitlines() if events.exists() else []
    return result, lines


def test_offsite_wrapper_verifies_readback_before_local_pruning(tmp_path):
    result, events = run_wrapper(tmp_path)

    assert result.returncode == 0, result.stderr
    assert [line.split(":", 1)[0] for line in events] == [
        "docker", "rclone", "rclone", "docker"
    ]
    assert "rclone:check --download" in events[2]
    assert "prune_backups" in events[3]


@pytest.mark.parametrize("failure", ["copy", "check"])
def test_offsite_failure_preserves_local_retention_set(tmp_path, failure):
    result, events = run_wrapper(tmp_path, failure=failure)

    assert result.returncode != 0
    assert not any("prune_backups" in line for line in events)
