"""Contract tests for the host-only, version-aware backup retention script."""
from pathlib import Path

from django.conf import settings

BASE = Path(settings.BASE_DIR)
SCRIPT = BASE / "scripts" / "operations" / "denstock-backup-capped"
INSTALLER = BASE / "scripts" / "operations" / "install-denstock-backup-capped.sh"


def test_retention_script_has_version_aware_accounting_and_policy():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--s3-versions" in text
    assert "86400" in text and "604800" in text
    assert 'newest="${runs[0]}"' in text
    assert "kept_days" in text and "kept_weeks" in text
    assert "838860800" in text


def test_retention_is_dry_run_safe_and_fails_closed():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'DRY_RUN=0' in text
    assert 'log "DRY-RUN: $*"' in text
    assert 'run rclone purge' in text
    assert 'run rclone cleanup' in text
    assert 'Unable to determine version-aware remote size.' in text
    assert 'exceeds hard limit' in text


def test_installer_validates_backs_up_and_replaces_atomically():
    text = INSTALLER.read_text(encoding="utf-8")
    for required in ("bash -n", "cp -p", "mktemp", "install -o root -g root -m 0755", "mv -f"):
        assert required in text


def test_docs_explain_versioned_bytes_and_safe_update():
    text = (BASE / "docs" / "operations" / "canonical-backup-retention.md").read_text(
        encoding="utf-8"
    )
    assert "--s3-versions" in text
    assert "asynchronously" in text
    assert "--dry-run" in text
