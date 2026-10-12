import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


def test_prune_backups_command_uses_backup_root(settings, tmp_path, monkeypatch):
    settings.BACKUP_ROOT = tmp_path
    removed_paths = [tmp_path / "old"]
    calls = []

    def prune(root, keep_last):
        calls.append((root, keep_last))
        return removed_paths

    monkeypatch.setattr("apps.operations.backup.prune_old_runs", prune)
    call_command("prune_backups", "--keep-last", "14")

    assert calls == [(tmp_path, 14)]


@pytest.mark.parametrize("keep_last", ["0", "-1"])
def test_prune_backups_command_rejects_non_positive_retention(keep_last):
    with pytest.raises(CommandError, match="больше нуля"):
        call_command("prune_backups", "--keep-last", keep_last)
