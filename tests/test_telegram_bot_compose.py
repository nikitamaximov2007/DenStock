"""Compose contract of the Telegram bot: bot-only egress, no AI-support access."""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "telegram-egress"))

import telegram_egress  # noqa: E402

COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
BOT = SERVICES["telegram-bot"]


def _environment(service) -> dict:
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        return dict(item.split("=", 1) for item in environment)
    return environment


def _env_files(service) -> list[str]:
    entries = service.get("env_file") or []
    if isinstance(entries, str):
        entries = [entries]
    return [entry["path"] if isinstance(entry, dict) else entry for entry in entries]


def test_bot_disables_ai_support_and_has_no_ai_support_mounts():
    assert _environment(BOT)["AI_SUPPORT_ENABLED"] == "false"
    mounts = " ".join(str(volume) for volume in BOT.get("volumes", []))
    assert "denstock-ai" not in mounts
    assert "launcher.sock" not in mounts


def test_only_the_bot_uses_the_telegram_egress_proxy():
    expected = (
        "${TELEGRAM_API_PROXY_URL-http://"
        f"{telegram_egress.BRIDGE_GATEWAY}:{telegram_egress.EGRESS_PORT}}}"
    )
    assert _environment(BOT)["TELEGRAM_API_PROXY_URL"] == expected
    assert set(BOT["networks"]) == {"default", "telegram-egress"}
    assert BOT["networks"]["telegram-egress"]["ipv4_address"] == telegram_egress.BOT_ADDRESS
    for name, service in SERVICES.items():
        if name == "telegram-bot":
            continue
        assert "TELEGRAM_API_PROXY_URL" not in _environment(service), name
        networks = service.get("networks") or {}
        assert "telegram-egress" not in networks, name


def test_only_the_bot_reads_the_telegram_secret_file():
    assert ".env.telegram" in _env_files(BOT)
    for name, service in SERVICES.items():
        if name != "telegram-bot":
            assert ".env.telegram" not in _env_files(service), name


def test_egress_network_is_internal_and_matches_the_proxy_firewall():
    network = COMPOSE["networks"]["telegram-egress"]
    assert network["internal"] is True
    assert network["driver"] == "bridge"
    assert network["driver_opts"]["com.docker.network.bridge.name"] == telegram_egress.BRIDGE_NAME
    assert network["ipam"]["config"] == [
        {"subnet": telegram_egress.BRIDGE_SUBNET, "gateway": telegram_egress.BRIDGE_GATEWAY}
    ]
