"""The MAX runbook must describe the tooling that actually exists, without secrets."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = (ROOT / "docs/operations/max-bot.md").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import max_release  # noqa: E402


def test_runbook_covers_every_release_section():
    for heading in (
        "Архитектура", "Сервисы", "Секреты", "Файлы на сервере", "Сертификат MAX",
        "Токен существующего бота", "GET /me", "Публичное имя", "Маршрут webhook",
        "Подписка webhook", "Здоровье", "Порядок запуска и остановки", "PRE",
        "Приёмка", "POST", "main после приёмки", "Откат", "Ротация", "репетиция",
    ):
        assert heading in RUNBOOK, heading


def test_every_tool_command_in_the_runbook_exists():
    used = set(re.findall(r"max_release\.py (?:--\S+ )*([a-z-]+)", RUNBOOK))
    assert used, "runbook names no release tool command"
    parser_text = (ROOT / "scripts/operations/max_release.py").read_text(encoding="utf-8")
    for command in used:
        assert f'"{command}"' in parser_text, command
    for command in ("max_bot_identity", "max_bot_health", "max_webhook"):
        assert (ROOT / f"apps/customer_requests/management/commands/{command}.py").is_file()
        assert command in RUNBOOK


def test_runbook_pins_the_facts_the_tool_enforces():
    assert max_release.PRE_MAX_CADDY_SHA256[:8] in RUNBOOK
    assert max_release.PUBLIC_WEBHOOK_URL in RUNBOOK
    assert max_release.CA_NAME in RUNBOOK


def test_runbook_carries_no_secret_values_and_no_em_dash():
    assert "—" not in RUNBOOK
    for key in ("MAX_BOT_TOKEN", "MAX_WEBHOOK_SECRET", "TELEGRAM_BOT_TOKEN"):
        for match in re.finditer(rf"{key}=(\S+)", RUNBOOK):
            assert match.group(1).startswith("<") or match.group(1) == "", match.group(0)
