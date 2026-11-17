import json
import os
import stat
from types import SimpleNamespace

import pytest
import telegram_egress as egress
from denstock_ai_network.constants import DEFAULT_PROXY_PORT, FIREWALL_TABLE


def test_egress_accepts_only_api_telegram_org_through_turkey_reverse_socks():
    config = egress.build_egress_config()

    assert config["inbounds"] == [
        {
            "listen": "10.231.0.1",
            "port": 2081,
            "protocol": "http",
            "tag": egress.INBOUND_TAG,
            "settings": {},
        }
    ]
    assert config["outbounds"] == [
        {
            "protocol": "socks",
            "tag": "turkey-reverse",
            "settings": {"servers": [{"address": "127.0.0.1", "port": 23082}]},
        },
        {"protocol": "blackhole", "tag": "blocked", "settings": {}},
    ]
    assert config["routing"]["rules"] == [
        {
            "type": "field",
            "inboundTag": [egress.INBOUND_TAG],
            "domain": ["full:api.telegram.org"],
            "port": "443",
            "outboundTag": "turkey-reverse",
        },
        {"type": "field", "inboundTag": [egress.INBOUND_TAG], "outboundTag": "blocked"},
    ]
    rendered = json.dumps(config).lower()
    for forbidden in ("vless", "maxinik", "sing-box", "0.0.0.0", '"tls"'):
        assert forbidden not in rendered


def test_generated_route_is_fixed_to_reverse_listener_and_never_uses_vless():
    config = egress.build_egress_config()

    assert config["routing"]["domainStrategy"] == "AsIs"
    assert config["outbounds"][0]["settings"]["servers"] == [
        {"address": "127.0.0.1", "port": 23082}
    ]
    assert all(item["protocol"] != "vless" for item in config["outbounds"])


def test_telegram_proxy_keeps_separate_listener_and_firewall_from_ai_proxy():
    config = egress.build_egress_config()

    assert egress.EGRESS_PORT != DEFAULT_PROXY_PORT
    assert egress.FIREWALL_TABLE != FIREWALL_TABLE
    assert config["inbounds"][0]["listen"] == "10.231.0.1"


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


def test_check_prints_only_a_redacted_summary(capsys):
    assert egress.main(["render", "--check", "--show-redacted"]) == 0

    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["client"] == "xray"
    assert summary["listen"] == "10.231.0.1:2081"
    assert summary["allowed"] == "api.telegram.org:443"
    assert summary["upstream"] == "127.0.0.1:23082"
    assert summary["tls_terminated"] is False


def test_apply_requires_root(monkeypatch, capsys):
    monkeypatch.setattr(egress.os, "geteuid", lambda: 1000)
    assert egress.main(["render", "--apply"]) == 2
    assert "root" in capsys.readouterr().err


def test_apply_tests_xray_and_nft_before_atomically_replacing_config(tmp_path, monkeypatch):
    config_path = tmp_path / "telegram-egress.json"
    nft_path = tmp_path / "telegram-egress.nft"
    config_path.write_text("old-config")
    nft_path.write_text("old-firewall")
    monkeypatch.setattr(egress, "EGRESS_CONFIG_PATH", config_path)
    monkeypatch.setattr(egress, "EGRESS_NFT_PATH", nft_path)
    monkeypatch.setattr(egress.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        egress.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
    )
    calls = []

    def runner(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    egress.render(SimpleNamespace(apply=True), runner=runner)

    assert calls[0][:4] == [
        str(egress.XRAY_BINARY), "run", "-test", "-config"
    ]
    assert calls[1][:3] == ["/usr/sbin/nft", "--check", "--file"]
    assert json.loads(config_path.read_text())["outbounds"][0]["protocol"] == "socks"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(nft_path.stat().st_mode) == 0o600


def test_apply_keeps_existing_files_when_xray_rejects_candidate(tmp_path, monkeypatch):
    config_path = tmp_path / "telegram-egress.json"
    nft_path = tmp_path / "telegram-egress.nft"
    config_path.write_text("old-config")
    nft_path.write_text("old-firewall")
    monkeypatch.setattr(egress, "EGRESS_CONFIG_PATH", config_path)
    monkeypatch.setattr(egress, "EGRESS_NFT_PATH", nft_path)
    monkeypatch.setattr(egress.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        egress.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
    )
    calls = []

    def runner(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=1)

    with pytest.raises(egress.EgressError, match="Xray rejected"):
        egress.render(SimpleNamespace(apply=True), runner=runner)

    assert len(calls) == 1
    assert config_path.read_text() == "old-config"
    assert nft_path.read_text() == "old-firewall"


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
        "ExecStartPre=/usr/local/lib/denstock-ai/bin/xray run -test -config "
        "/etc/denstock-ai/telegram-egress.json",
        "ExecStart=/usr/local/lib/denstock-ai/bin/xray run -config "
        "/etc/denstock-ai/telegram-egress.json",
        "ExecStopPost=-+/usr/sbin/nft delete table inet denstock_telegram_egress",
        "ReadOnlyPaths=/etc/denstock-ai/telegram-egress.json",
        "Restart=on-failure",
    ):
        assert directive in unit
    for forbidden in ("sing-box", "0.0.0.0", "2080", "denstock-ai-launcher", "Before="):
        assert forbidden not in unit


def test_egress_tooling_has_no_shell(project_root):
    text = (project_root / "scripts" / "telegram-egress" / "telegram_egress.py").read_text()
    assert "shell=True" not in text
    assert "os.system" not in text
    assert '"/usr/bin/docker"' not in text
