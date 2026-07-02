"""v1.1.8B — scaffold автоматического offsite-бэкапа (host-level).

Тесты НЕ запускают Docker/rclone: проверяют только наличие/безопасность файлов scaffold.
"""
from pathlib import Path

from django.conf import settings

BASE = Path(settings.BASE_DIR)
SCRIPT = BASE / "scripts" / "operations" / "backup_offsite.sh"


def test_offsite_script_exists_with_shebang():
    assert SCRIPT.exists()
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


def test_offsite_script_is_safe():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    # host-скрипт НЕ восстанавливает и не трогает секреты приложения.
    assert "restore_db" not in text
    assert "restore_media" not in text
    assert ". ./.env " not in text  # не читаем основной .env (только .env.backup)


def test_env_backup_example_exists_without_secrets():
    text = (BASE / ".env.backup.example").read_text(encoding="utf-8")
    for key in (
        "BACKUP_KEEP_LAST", "BACKUP_OFFSITE_ENABLED", "BACKUP_OFFSITE_METHOD",
        "BACKUP_OFFSITE_TARGET", "BACKUP_STATUS_FILE",
    ):
        assert key in text
    lowered = text.lower()
    assert "password" not in lowered
    assert "secret_key" not in lowered
    assert "token" not in lowered


def test_gitignore_env_backup_rules():
    text = (BASE / ".gitignore").read_text(encoding="utf-8")
    assert ".env.*" in text  # реальный .env.backup игнорируется
    assert "!.env.backup.example" in text  # пример коммитится
    assert "/backups/" in text  # выгрузки не в Git


def test_scheduled_offsite_doc_exists():
    assert (BASE / "docs" / "operations" / "scheduled-offsite-backups.md").exists()
