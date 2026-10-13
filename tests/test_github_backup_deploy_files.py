"""Static guarantees of the GitHub backup schedule and monitors (not installed)."""

from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/github-backup"


def test_service_runs_after_the_nightly_backup_and_tolerates_a_held_lock():
    unit = (DEPLOY / "systemd/denstock-github-backup.service").read_text()
    assert "After=denstock-backup.service" in unit
    assert "SuccessExitStatus=75" in unit
    # Installed outside the /opt/denstock git tree, so a checkout never touches it.
    script = "/usr/local/lib/denstock/github_release_backup.py"
    assert f"ExecStart=/usr/bin/python3 {script} push" in unit
    assert "OnFailure=denstock-github-backup-alert.service" in unit
    assert "ProtectSystem=strict" in unit and "NoNewPrivileges=yes" in unit
    assert "Environment=GHB_WORK_DIR=/var/lib/denstock-github-backup" in unit
    assert "StateDirectory=denstock-github-backup" in unit
    assert "TOKEN" not in unit.upper().replace("GHB_CONFIG", "")


def test_timer_is_daily_and_catches_up_after_downtime():
    timer = (DEPLOY / "systemd/denstock-github-backup.timer").read_text()
    assert "OnCalendar=*-*-* 05:00:00 Europe/Moscow" in timer
    assert "Persistent=true" in timer


def test_actions_monitor_is_read_only_and_never_echoes_the_token():
    workflow = (DEPLOY / "backup-repo/.github/workflows/backup-freshness.yml").read_text()
    assert "permissions:\n  contents: read" in workflow
    assert "write" not in workflow.split("permissions:", 1)[1].split("jobs:", 1)[0]
    assert "check --max-age-hours" in workflow
    assert "echo \"$TOKEN\"" not in workflow and "set -x" not in workflow
