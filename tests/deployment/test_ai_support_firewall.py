import subprocess
from types import SimpleNamespace

import pytest
from denstock_ai_network.firewall import (
    ALLOW_COMMENT,
    BLOCK_COMMENT,
    OTHER_USERS_COMMENT,
    FirewallError,
    remove_policy,
    render_nftables,
    validate_policy_text,
)
from denstock_ai_network.health import check_health


def completed(returncode=0, stdout=b""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=b"")


def test_firewall_allows_only_the_uid_loopback_proxy():
    policy = render_nftables(1201, 2080)

    assert "meta skuid 1201 ip daddr 127.0.0.1 tcp dport 2080" in policy
    assert ALLOW_COMMENT in policy
    assert "meta skuid 1201 counter reject" in policy
    assert BLOCK_COMMENT in policy


def test_firewall_final_uid_reject_covers_ipv4_ipv6_udp_and_dns():
    policy = render_nftables(1201, 2080)

    assert "table inet denstock_ai" in policy
    assert "meta skuid 1201 counter reject" in policy
    assert "udp dport 53 accept" not in policy
    assert "ip6" not in policy
    assert "ct state established accept" not in policy


def test_firewall_does_not_change_routes_or_global_policy():
    policy = render_nftables(1201, 2080).lower()

    assert "policy accept" in policy
    assert "ip route" not in policy
    assert "ip rule" not in policy
    assert "masquerade" not in policy
    assert "0.0.0.0" not in policy


def test_firewall_blocks_local_proxy_for_other_non_root_users_only():
    policy = render_nftables(1201, 2080)

    assert "meta skuid != 0 meta skuid != 1201" in policy
    assert "127.0.0.1 tcp dport 2080" in policy
    assert OTHER_USERS_COMMENT in policy
    assert "policy drop" not in policy


def test_firewall_replace_is_an_atomic_table_flush_batch():
    policy = render_nftables(1201, 2080, replace_existing=True)

    assert policy.startswith("flush table inet denstock_ai\n\ntable inet denstock_ai")
    validate_policy_text(policy, 1201, 2080)


def test_firewall_validator_rejects_incomplete_policy():
    with pytest.raises(FirewallError):
        validate_policy_text("table inet denstock_ai {}", 1201, 2080)


def test_firewall_rollback_is_idempotent_when_table_is_absent():
    calls = []

    def runner(argv):
        calls.append(argv)
        return completed(returncode=1)

    assert remove_policy(dry_run=False, runner=runner) == "already absent"
    assert len(calls) == 1


@pytest.fixture
def launcher_config():
    return SimpleNamespace(
        proxy_service="denstock-ai-proxy.service",
        proxy_port=2080,
        proxy_host="127.0.0.1",
        firewall_table="denstock_ai",
    )


def healthy_runner(argv):
    if argv[0] == "/usr/sbin/nft":
        text = (
            "meta skuid 1201 ip daddr 127.0.0.1 tcp dport 2080 accept "
            f'comment "{ALLOW_COMMENT}"\n'
            "meta skuid != 0 meta skuid != 1201 ip daddr 127.0.0.1 "
            f'tcp dport 2080 reject comment "{OTHER_USERS_COMMENT}"\n'
            f'meta skuid 1201 reject comment "{BLOCK_COMMENT}"\n'
        )
        return completed(stdout=text.encode())
    if argv[0] == "/usr/bin/ss":
        return completed(stdout=b"LISTEN 0 4096 127.0.0.1:2080 0.0.0.0:*\n")
    return completed()


def test_health_is_ok_only_with_firewall_local_bind_and_proxy_route(launcher_config):
    result = check_health(
        launcher_config,
        ai_uid=1201,
        runner=healthy_runner,
        proxy_connect=lambda *_args: True,
    )

    assert result.status == "ok"
    assert result.direct_network_blocked is True


def test_health_proxy_down_remains_fail_closed(launcher_config):
    def runner(argv):
        result = healthy_runner(argv)
        if argv[0] == "/usr/bin/systemctl":
            return completed(returncode=3)
        return result

    result = check_health(
        launcher_config,
        ai_uid=1201,
        runner=runner,
        proxy_connect=lambda *_args: pytest.fail("route probe must not run"),
    )

    assert result.status == "proxy_unavailable"
    assert result.direct_network_blocked is True


def test_health_missing_firewall_fails_before_proxy_probe(launcher_config):
    def runner(argv):
        if argv[0] == "/usr/sbin/nft":
            return completed(returncode=1)
        return healthy_runner(argv)

    result = check_health(
        launcher_config,
        ai_uid=1201,
        runner=runner,
        proxy_connect=lambda *_args: pytest.fail("route probe must not run"),
    )

    assert result.status == "direct_network_not_blocked"
    assert result.direct_network_blocked is False


def test_health_rejects_firewall_chain_with_an_extra_bypass_rule(launcher_config):
    def runner(argv):
        result = healthy_runner(argv)
        if argv[0] == "/usr/sbin/nft":
            result.stdout += b"meta skuid 1201 accept\n"
        return result

    result = check_health(
        launcher_config,
        ai_uid=1201,
        runner=runner,
        proxy_connect=lambda *_args: pytest.fail("route probe must not run"),
    )

    assert result.status == "direct_network_not_blocked"
    assert result.direct_network_blocked is False


def test_health_rejects_non_loopback_listener(launcher_config):
    def runner(argv):
        if argv[0] == "/usr/bin/ss":
            return completed(stdout=b"LISTEN 0 4096 0.0.0.0:2080 0.0.0.0:*\n")
        return healthy_runner(argv)

    result = check_health(
        launcher_config,
        ai_uid=1201,
        runner=runner,
        proxy_connect=lambda *_args: pytest.fail("route probe must not run"),
    )

    assert result.status == "proxy_unavailable"


def test_health_rejects_unexpected_proxy_egress(launcher_config):
    result = check_health(
        launcher_config,
        ai_uid=1201,
        runner=healthy_runner,
        proxy_connect=lambda *_args: True,
        egress_validator=lambda: False,
    )

    assert result.status == "unexpected_egress"
    assert result.direct_network_blocked is True


def test_health_has_no_direct_fallback_after_proxy_failure(launcher_config):
    result = check_health(
        launcher_config,
        ai_uid=1201,
        runner=healthy_runner,
        proxy_connect=lambda *_args: False,
    )

    assert result.status == "proxy_unavailable"
    assert result.direct_network_blocked is True
