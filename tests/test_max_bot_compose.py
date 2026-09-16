"""Compose contract of MAX: one worker service, each secret in exactly one service.

Least privilege by construction: max-bot alone reads the bot token, web alone
reads the webhook secret, catalog-web gets the public username only, and the
Telegram bot keeps its own secret without either MAX one. MAX is reached
directly: max-bot joins no egress network and inherits no proxy.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
BOT = SERVICES["max-bot"]


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


def test_max_bot_runs_only_the_worker_behind_its_own_profile():
    assert BOT["profiles"] == ["max-bot"]
    assert BOT["entrypoint"] == ["python", "manage.py"]
    assert BOT["command"] == ["run_max_bot"]
    assert "ports" not in BOT and "expose" not in BOT
    assert BOT["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert BOT["restart"] == "unless-stopped"
    assert "deploy" not in BOT or "replicas" not in BOT.get("deploy", {})


def test_max_bot_healthcheck_proves_heartbeat_and_lease_without_secrets():
    check = BOT["healthcheck"]
    assert check["test"] == ["CMD", "python", "manage.py", "max_bot_health"]
    assert check["start_period"] and check["retries"] >= 3
    command = " ".join(check["test"])
    for word in ("TOKEN", "SECRET", "curl", "messages"):
        assert word not in command
    assert _environment(BOT)["MAX_BOT_HEARTBEAT_FILE"].startswith("/tmp/")


def test_only_max_bot_reads_the_max_token_file():
    assert _env_files(BOT) == [".env", ".env.max"]
    optional = [entry for entry in BOT["env_file"] if isinstance(entry, dict)]
    assert {"path": ".env.max", "required": False} in optional
    for name, service in SERVICES.items():
        if name != "max-bot":
            assert ".env.max" not in _env_files(service), name


def test_only_web_reads_the_webhook_secret_file():
    assert _env_files(SERVICES["web"]) == [".env", ".env.max-webhook"]
    for name, service in SERVICES.items():
        if name != "web":
            assert ".env.max-webhook" not in _env_files(service), name


def test_every_other_service_has_the_max_secrets_emptied():
    assert _environment(SERVICES["web"])["MAX_BOT_TOKEN"] == ""
    for name in ("catalog-web", "telegram-bot"):
        environment = _environment(SERVICES[name])
        assert environment["MAX_BOT_TOKEN"] == "", name
        assert environment["MAX_WEBHOOK_SECRET"] == "", name
    bot = _environment(BOT)
    assert bot["MAX_WEBHOOK_SECRET"] == "" and bot["MAX_WEBHOOK_ENABLED"] == "false"
    assert bot["TELEGRAM_BOT_TOKEN"] == ""
    assert bot["AI_SUPPORT_ENABLED"] == "false"


def test_catalog_web_gets_the_username_from_its_own_file_only():
    catalog = SERVICES["catalog-web"]
    assert _env_files(catalog) == [".env.public"]
    public_settings = (ROOT / "config/settings/public.py").read_text(encoding="utf-8")
    assert 'MAX_BOT_TOKEN = ""' in public_settings
    assert 'MAX_WEBHOOK_SECRET = ""' in public_settings


def test_telegram_bot_keeps_only_its_own_secret_file():
    assert _env_files(SERVICES["telegram-bot"]) == [".env", ".env.telegram"]


def test_max_bot_is_direct_no_egress_network_and_no_proxy():
    assert "networks" not in BOT  # the default network only
    environment = _environment(BOT)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert environment[name] == "", name
    assert "TELEGRAM_API_PROXY_URL" not in environment
    mounts = " ".join(str(volume) for volume in BOT.get("volumes", []))
    assert "denstock-ai" not in mounts and "launcher.sock" not in mounts


def test_max_bot_mounts_only_the_public_ca_directory_read_only():
    (volume,) = BOT["volumes"]
    assert volume["type"] == "bind"
    assert volume["target"] == "/etc/denstock/max"
    assert volume["read_only"] is True
    assert volume["bind"]["create_host_path"] is False
    assert volume["source"] == "${DENSTOCK_MAX_CA_DIR:-/etc/denstock/max}"


def test_db_and_proxy_hold_no_messenger_secret():
    for name in ("db", "proxy"):
        service = SERVICES[name]
        assert not _env_files(service), name
        text = yaml.safe_dump(service)
        assert "TOKEN" not in text and "MAX_WEBHOOK" not in text, name


def test_env_example_places_every_max_value_in_its_own_file():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "/opt/denstock/.env.max (root, chmod 600) читает ТОЛЬКО max-bot" in text
    assert "/opt/denstock/.env.max-webhook (root, chmod 600) читает ТОЛЬКО web" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("MAX_BOT_TOKEN", "MAX_WEBHOOK_SECRET")):
            raise AssertionError(f"MAX secret key outside a comment: {stripped}")
