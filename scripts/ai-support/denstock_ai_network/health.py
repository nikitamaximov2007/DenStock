import socket
import subprocess
from dataclasses import dataclass

from .config import LauncherConfig
from .constants import HEALTH_STATUSES
from .firewall import ALLOW_COMMENT, BLOCK_COMMENT, OTHER_USERS_COMMENT


@dataclass(frozen=True)
class HealthResult:
    status: str
    direct_network_blocked: bool

    def __post_init__(self):
        if self.status not in HEALTH_STATUSES:
            raise ValueError("invalid health status")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)


def _proxy_connect(host: str, port: int, timeout: float) -> bool:
    target = "chatgpt.com:443"
    request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n"
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(request.encode("ascii"))
            response = connection.recv(1024)
    except OSError:
        return False
    first_line = response.split(b"\r\n", 1)[0]
    return first_line.startswith((b"HTTP/1.1 200 ", b"HTTP/1.0 200 "))


def _firewall_matches(text: str, *, ai_uid: int, proxy_port: int) -> bool:
    rule_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith(("table ", "chain ", "type "))
            or line in {"{", "}"}
        ):
            continue
        rule_lines.append(line)
    if len(rule_lines) != 3:
        return False
    by_comment = {}
    for comment in (ALLOW_COMMENT, OTHER_USERS_COMMENT, BLOCK_COMMENT):
        matches = [line for line in rule_lines if f'comment "{comment}"' in line]
        if len(matches) != 1:
            return False
        by_comment[comment] = matches[0]
    allow = by_comment[ALLOW_COMMENT]
    other_users = by_comment[OTHER_USERS_COMMENT]
    block = by_comment[BLOCK_COMMENT]
    return (
        f"meta skuid {ai_uid}" in allow
        and "ip daddr 127.0.0.1" in allow
        and f"tcp dport {proxy_port}" in allow
        and " accept " in f" {allow} "
        and "meta skuid != 0" in other_users
        and f"meta skuid != {ai_uid}" in other_users
        and "ip daddr 127.0.0.1" in other_users
        and f"tcp dport {proxy_port}" in other_users
        and " reject " in f" {other_users} "
        and f"meta skuid {ai_uid}" in block
        and " reject " in f" {block} "
    )


def check_health(
    config: LauncherConfig,
    *,
    ai_uid: int,
    runner=_run,
    proxy_connect=_proxy_connect,
    egress_validator=None,
) -> HealthResult:
    try:
        firewall = runner(
            [
                "/usr/sbin/nft",
                "-nn",
                "list",
                "chain",
                "inet",
                config.firewall_table,
                "output",
            ]
        )
        firewall_text = firewall.stdout.decode("utf-8", "replace")
        direct_blocked = firewall.returncode == 0 and _firewall_matches(
            firewall_text,
            ai_uid=ai_uid,
            proxy_port=config.proxy_port,
        )
        if not direct_blocked:
            return HealthResult("direct_network_not_blocked", False)

        active = runner(["/usr/bin/systemctl", "is-active", "--quiet", config.proxy_service])
        if active.returncode != 0:
            return HealthResult("proxy_unavailable", True)
        listeners = runner(
            ["/usr/bin/ss", "-H", "-ltn", f"sport = :{config.proxy_port}"]
        )
        listener_text = listeners.stdout.decode("utf-8", "replace")
        listener_lines = [line for line in listener_text.splitlines() if line.strip()]
        expected_endpoint = f"{config.proxy_host}:{config.proxy_port}"
        local_endpoint = listener_lines[0].split()[3] if len(listener_lines) == 1 else ""
        if (
            listeners.returncode != 0
            or len(listener_lines) != 1
            or local_endpoint != expected_endpoint
        ):
            return HealthResult("proxy_unavailable", True)
        if not proxy_connect(config.proxy_host, config.proxy_port, 5.0):
            return HealthResult("proxy_unavailable", True)
        if egress_validator is not None and not egress_validator():
            return HealthResult("unexpected_egress", True)
        return HealthResult("ok", True)
    except (OSError, UnicodeError, ValueError):
        return HealthResult("configuration_error", False)
