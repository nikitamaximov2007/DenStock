import json
from io import StringIO

import pytest
import telegram_egress as egress
from denstock_ai_network.constants import DEFAULT_PROXY_PORT, FIREWALL_TABLE
from denstock_ai_network.firewall import render_nftables as render_ai_nftables
from denstock_ai_network.renderer import build_sing_box_config, validate_values

SECRET_UUID = "00000000-0000-4000-8000-000000000001"
SECRET_KEY = "A" * 43


@pytest.fixture
def values():
    return validate_values(
        {
            "MAXINIK_SERVER": "vpn.invalid",
            "MAXINIK_PORT": "443",
            "MAXINIK_UUID": SECRET_UUID,
            "MAXINIK_FLOW": "xtls-rprx-vision",
            "MAXINIK_REALITY_PUBLIC_KEY": SECRET_KEY,
            "MAXINIK_REALITY_SHORT_ID": "0123456789abcdef",
            "MAXINIK_REALITY_SNI": "cover.invalid",
            "MAXINIK_FINGERPRINT": "chrome",
            "MAXINIK_LOCAL_PROXY_HOST": "127.0.0.1",
            "MAXINIK_LOCAL_PROXY_PORT": "2080",
        }
    )


def _env_text(values):
    raw = {
        "MAXINIK_SERVER": "vpn.invalid",
        "MAXINIK_PORT": "443",
        "MAXINIK_UUID": SECRET_UUID,
        "MAXINIK_FLOW": "xtls-rprx-vision",
        "MAXINIK_REALITY_PUBLIC_KEY": SECRET_KEY,
        "MAXINIK_REALITY_SHORT_ID": "0123456789abcdef",
        "MAXINIK_REALITY_SNI": "cover.invalid",
        "MAXINIK_FINGERPRINT": "chrome",
        "MAXINIK_LOCAL_PROXY_HOST": "127.0.0.1",
        "MAXINIK_LOCAL_PROXY_PORT": "2080",
    }
    return "\n".join(f"{key}={value}" for key, value in raw.items()) + "\n"


def test_egress_accepts_only_api_telegram_org_through_the_same_outbound(values):
    config = egress.build_egress_config(values)

    assert config["inbounds"] == [
        {
            "type": "http",
            "tag": egress.INBOUND_TAG,
            "listen": "10.231.0.1",
            "listen_port": 2081,
            "set_system_proxy": False,
        }
    ]
    assert config["outbounds"] == build_sing_box_config(values)["outbounds"]
    assert config["route"]["rules"] == [
        {
            "inbound": [egress.INBOUND_TAG],
            "domain": ["api.telegram.org"],
            "port": [443],
            "action": "route",
            "outbound": "maxinik-vless",
        },
        {"action": "reject"},
    ]
    inbound = json.dumps(config["inbounds"]).lower()
    for forbidden in ('"mixed"', '"socks"', '"tun"', "0.0.0.0", "127.0.0.1", '"tls"'):
        assert forbidden not in inbound


def test_ai_proxy_configuration_and_firewall_are_unchanged(values):
    ai = build_sing_box_config(values)

    assert [(item["type"], item["listen"]) for item in ai["inbounds"]] == [("mixed", "127.0.0.1")]
    assert "rules" not in ai["route"]
    assert egress.EGRESS_PORT != DEFAULT_PROXY_PORT
    assert egress.FIREWALL_TABLE != FIREWALL_TABLE
    assert egress.EGRESS_CONFIG_PATH.name != "sing-box.json"
    assert egress.FIREWALL_TABLE not in render_ai_nftables(1201, 2080)


def test_egress_firewall_allows_only_the_bot_on_its_bridge():
    text = egress.render_nftables()
    egress.validate_nftables(text)

    assert (
        'iifname "br-tg-egress" ip saddr 10.231.0.2 ip daddr 10.231.0.1 tcp dport 2081 '
        "counter accept"
    ) in text
    assert "tcp dport 2081 counter drop" in text
    assert "hook input" in text
    assert "hook output" not in text
    assert "policy accept" in text


@pytest.mark.parametrize(
    "addition",
    [
        "masquerade",
        "dnat to 1.2.3.4",
        "ip daddr 0.0.0.0",
        "policy drop;",
        "table inet denstock_ai {",
    ],
)
def test_egress_firewall_validator_rejects_global_or_foreign_rules(addition):
    with pytest.raises(egress.EgressError):
        egress.validate_nftables(egress.render_nftables() + f"\n{addition}\n")


def test_check_prints_only_a_redacted_summary(values, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", StringIO(_env_text(values)))

    assert egress.main(["render", "--stdin", "--check", "--show-redacted"]) == 0

    out = capsys.readouterr().out
    assert SECRET_UUID not in out and SECRET_KEY not in out and "vpn.invalid" not in out
    summary = json.loads(out)
    assert summary["listen"] == "10.231.0.1:2081"
    assert summary["allowed"] == "api.telegram.org:443"
    assert summary["tls_terminated"] is False


def test_apply_requires_root_and_a_fixed_secret_source(values, monkeypatch, capsys):
    monkeypatch.setattr(egress.os, "geteuid", lambda: 1000)
    monkeypatch.setattr("sys.stdin", StringIO(_env_text(values)))
    assert egress.main(["render", "--stdin", "--apply"]) == 2
    assert "root" in capsys.readouterr().err

    assert egress.main(["render", "--env-file", "/tmp/other.env", "--check"]) == 2
    assert "maxinik.env" in capsys.readouterr().err


def test_install_unit_dry_run_never_enables_or_starts(capsys):
    assert egress.main(["install-unit", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan[-1] == "do not enable or start the service"
    assert not any("systemctl enable" in step or "systemctl start" in step for step in plan)


def test_unit_is_hardened_and_bound_only_to_the_egress_files(project_root):
    unit = (project_root / "deploy" / "telegram-egress" / egress.UNIT_NAME).read_text()

    for directive in (
        "User=denstock-ai-proxy",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "MemoryDenyWriteExecute=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "After=network-online.target docker.service",
        "PartOf=docker.service",
        "ExecStartPre=+/usr/sbin/nft --file /etc/denstock-ai/telegram-egress.nft",
        "ExecStartPre=/usr/local/lib/denstock-ai/bin/sing-box check --config "
        "/etc/denstock-ai/telegram-egress.json",
        "ExecStart=/usr/local/lib/denstock-ai/bin/sing-box run --config "
        "/etc/denstock-ai/telegram-egress.json",
        "ExecStopPost=-+/usr/sbin/nft delete table inet denstock_telegram_egress",
        "ReadOnlyPaths=/etc/denstock-ai/telegram-egress.json",
        "Restart=on-failure",
    ):
        assert directive in unit
    for forbidden in ("sing-box.json", "0.0.0.0", "2080", "denstock-ai-launcher", "Before="):
        assert forbidden not in unit


def test_egress_tooling_has_no_shell(project_root):
    text = (project_root / "scripts" / "telegram-egress" / "telegram_egress.py").read_text()
    assert "shell=True" not in text
    assert "os.system" not in text
    assert '"/usr/bin/docker"' not in text
