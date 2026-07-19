import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import LauncherConfigurationError, load_launcher_config
from .constants import AI_USER, FIREWALL_TABLE, LAUNCHER_CONFIG_PATH, NFTABLES_CONFIG_PATH

try:
    import pwd
except ImportError:  # pragma: no cover - exercised only by Linux deployment
    pwd = None

ALLOW_COMMENT = "denstock-ai:allow-loopback-proxy"
OTHER_USERS_COMMENT = "denstock-ai:block-other-local-users"
BLOCK_COMMENT = "denstock-ai:block-all-direct"


class FirewallError(RuntimeError):
    pass


def render_nftables(uid: int, port: int, *, replace_existing: bool = False) -> str:
    if uid <= 0 or not 1024 <= port <= 65535:
        raise FirewallError("invalid firewall identity or port")
    prefix = f"flush table inet {FIREWALL_TABLE}\n\n" if replace_existing else ""
    allow_rule = (
        f"meta skuid {uid} ip daddr 127.0.0.1 tcp dport {port} counter accept "
        f'comment "{ALLOW_COMMENT}"'
    )
    other_users_rule = (
        f"meta skuid != 0 meta skuid != {uid} ip daddr 127.0.0.1 tcp dport {port} "
        f'counter reject with icmpx type admin-prohibited comment "{OTHER_USERS_COMMENT}"'
    )
    block_rule = (
        f"meta skuid {uid} counter reject with icmpx type admin-prohibited "
        f'comment "{BLOCK_COMMENT}"'
    )
    return prefix + f"""table inet {FIREWALL_TABLE} {{
    chain output {{
        type filter hook output priority -150; policy accept;

        {allow_rule}
        {other_users_rule}
        {block_rule}
    }}
}}
"""


def validate_policy_text(text: str, uid: int, port: int) -> None:
    required = (
        f"table inet {FIREWALL_TABLE}",
        "type filter hook output",
        f"meta skuid {uid} ip daddr 127.0.0.1 tcp dport {port}",
        ALLOW_COMMENT,
        OTHER_USERS_COMMENT,
        f"meta skuid {uid} counter reject",
        BLOCK_COMMENT,
    )
    if any(item not in text for item in required):
        raise FirewallError("firewall policy is incomplete")
    forbidden = ("0.0.0.0", "::/0 accept", "masquerade", "dnat", "snat", "ip route", "ip rule")
    if any(item in text.lower() for item in forbidden):
        raise FirewallError("firewall policy contains a forbidden global rule")


def _write_private(path: Path, content: str) -> None:
    if path.exists() and path.is_symlink():
        raise FirewallError("nftables output path is a symlink")
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as file:
            temporary = file.name
            os.chmod(temporary, 0o600)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise FirewallError("nftables policy could not be written") from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)


def table_exists(runner=_run) -> bool:
    result = runner(["/usr/sbin/nft", "list", "table", "inet", FIREWALL_TABLE])
    return result.returncode == 0


def install_policy(*, dry_run: bool, runner=_run) -> str:
    config = load_launcher_config(LAUNCHER_CONFIG_PATH)
    if pwd is None:
        raise FirewallError("Linux identity database is unavailable")
    uid = pwd.getpwnam(AI_USER).pw_uid
    policy = render_nftables(uid, config.proxy_port, replace_existing=table_exists(runner))
    validate_policy_text(policy, uid, config.proxy_port)
    if dry_run:
        return policy
    _write_private(NFTABLES_CONFIG_PATH, policy)
    checked = runner(["/usr/sbin/nft", "--check", "--file", str(NFTABLES_CONFIG_PATH)])
    if checked.returncode != 0:
        raise FirewallError("nftables rejected the policy")
    applied = runner(["/usr/sbin/nft", "--file", str(NFTABLES_CONFIG_PATH)])
    if applied.returncode != 0:
        raise FirewallError("nftables policy could not be applied")
    return "installed"


def remove_policy(*, dry_run: bool, runner=_run) -> str:
    if not table_exists(runner):
        if not dry_run:
            try:
                NFTABLES_CONFIG_PATH.unlink(missing_ok=True)
            except OSError as exc:
                raise FirewallError("nftables runtime policy could not be removed") from exc
        return "already absent"
    command = ["/usr/sbin/nft", "delete", "table", "inet", FIREWALL_TABLE]
    if dry_run:
        return " ".join(command)
    result = runner(command)
    if result.returncode != 0:
        raise FirewallError("nftables policy could not be removed")
    try:
        NFTABLES_CONFIG_PATH.unlink(missing_ok=True)
    except OSError as exc:
        raise FirewallError("nftables runtime policy could not be removed") from exc
    return "removed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the isolated denstock-ai nftables table")
    parser.add_argument("operation", choices=("install", "remove"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "posix" or (not args.dry_run and os.geteuid() != 0):
        print("configuration_error: Linux root is required", file=sys.stderr)
        return 2
    try:
        result = (
            install_policy(dry_run=args.dry_run)
            if args.operation == "install"
            else remove_policy(dry_run=args.dry_run)
        )
        print(result)
        return 0
    except (FirewallError, LauncherConfigurationError, KeyError) as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
