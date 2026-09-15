#!/usr/bin/python3
"""Render and install the dedicated Telegram Bot API egress proxy.

The production host cannot reach api.telegram.org directly. The telegram-bot
container reaches it through a second, separate sing-box process that shares
only the installed sing-box binary, the proxy system user and the validated
MAXINIK outbound with the AI proxy. The AI proxy itself (its config, its
127.0.0.1 listener, its nftables table and its health contract) is untouched.

This process:

* listens only on the gateway of the internal Docker bridge ``br-tg-egress``;
* routes only CONNECT to ``api.telegram.org:443`` and rejects everything else;
* is reachable only from the telegram-bot container address (own nftables table);
* never terminates TLS, so the bot token stays inside TLS to Telegram.

Runtime needs only the rendered files and the installed binary; this script is
run from the release checkout and never enables or starts a service.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

AI_INSTALL_ROOT = Path("/usr/local/lib/denstock-ai")
CONFIG_ROOT = Path("/etc/denstock-ai")
MAXINIK_ENV_PATH = CONFIG_ROOT / "maxinik.env"
EGRESS_CONFIG_PATH = CONFIG_ROOT / "telegram-egress.json"
EGRESS_NFT_PATH = CONFIG_ROOT / "telegram-egress.nft"
UNIT_NAME = "denstock-telegram-egress.service"
UNIT_SOURCE = Path(__file__).resolve().parents[2] / "deploy" / "telegram-egress" / UNIT_NAME
UNIT_TARGET = Path("/etc/systemd/system") / UNIT_NAME
PROXY_USER = "denstock-ai-proxy"

# Must match docker-compose.yml (network telegram-egress, service telegram-bot).
BRIDGE_NAME = "br-tg-egress"
BRIDGE_SUBNET = "10.231.0.0/29"
BRIDGE_GATEWAY = "10.231.0.1"
BOT_ADDRESS = "10.231.0.2"
EGRESS_PORT = 2081
ALLOWED_DOMAIN = "api.telegram.org"
ALLOWED_PORT = 443
INBOUND_TAG = "telegram-bot-http"
FIREWALL_TABLE = "denstock_telegram_egress"
ALLOW_COMMENT = "telegram-egress:allow-bot"
BLOCK_COMMENT = "telegram-egress:block-others"

try:
    import pwd
except ImportError:  # pragma: no cover - exercised only by Linux deployment
    pwd = None


class EgressError(RuntimeError):
    pass


def _renderer():
    """The installed, audited MAXINIK renderer: one validation, one outbound."""
    if AI_INSTALL_ROOT.is_dir() and str(AI_INSTALL_ROOT) not in sys.path:
        sys.path.insert(0, str(AI_INSTALL_ROOT))
    from denstock_ai_network import renderer

    return renderer


def build_egress_config(values: dict[str, object]) -> dict[str, object]:
    ai_config = _renderer().build_sing_box_config(values)
    outbound = next(item for item in ai_config["outbounds"] if item["tag"] == "maxinik-vless")
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "http",
                "tag": INBOUND_TAG,
                "listen": BRIDGE_GATEWAY,
                "listen_port": EGRESS_PORT,
                "set_system_proxy": False,
            }
        ],
        "outbounds": [outbound],
        "route": {
            "rules": [
                {
                    "inbound": [INBOUND_TAG],
                    "domain": [ALLOWED_DOMAIN],
                    "port": [ALLOWED_PORT],
                    "action": "route",
                    "outbound": outbound["tag"],
                },
                {"action": "reject"},
            ],
            "final": outbound["tag"],
            "auto_detect_interface": True,
        },
    }


def render_nftables() -> str:
    allow_rule = (
        f'iifname "{BRIDGE_NAME}" ip saddr {BOT_ADDRESS} ip daddr {BRIDGE_GATEWAY} '
        f'tcp dport {EGRESS_PORT} counter accept comment "{ALLOW_COMMENT}"'
    )
    block_rule = f'tcp dport {EGRESS_PORT} counter drop comment "{BLOCK_COMMENT}"'
    return f"""add table inet {FIREWALL_TABLE}
flush table inet {FIREWALL_TABLE}
table inet {FIREWALL_TABLE} {{
    chain input {{
        type filter hook input priority -150; policy accept;

        {allow_rule}
        {block_rule}
    }}
}}
"""


def validate_nftables(text: str) -> None:
    required = (
        f"table inet {FIREWALL_TABLE} {{",
        "type filter hook input",
        f'iifname "{BRIDGE_NAME}" ip saddr {BOT_ADDRESS} ip daddr {BRIDGE_GATEWAY} '
        f"tcp dport {EGRESS_PORT} counter accept",
        f"tcp dport {EGRESS_PORT} counter drop",
        ALLOW_COMMENT,
        BLOCK_COMMENT,
    )
    if any(item not in text for item in required):
        raise EgressError("egress firewall policy is incomplete")
    lowered = text.lower()
    forbidden = (
        "0.0.0.0", "::/0", "masquerade", "dnat", "snat", "policy drop",
        "hook output", "hook forward", "hook prerouting", "hook postrouting",
        "table inet denstock_ai ", "table inet denstock_ai {", "ip route", "ip rule",
    )
    if any(item in lowered for item in forbidden):
        raise EgressError("egress firewall policy contains a forbidden rule")


def redacted_summary(config: dict[str, object]) -> dict[str, object]:
    inbound = config["inbounds"][0]
    return {
        "client": "sing-box",
        "listen": f"{inbound['listen']}:{inbound['listen_port']}",
        "allowed": f"{ALLOWED_DOMAIN}:{ALLOWED_PORT}",
        "client_address": BOT_ADDRESS,
        "bridge": BRIDGE_NAME,
        "firewall_table": FIREWALL_TABLE,
        "tls_terminated": False,
        "secrets": "redacted",
    }


def _write_root_private(path: Path, content: str) -> None:
    if path.exists() and path.is_symlink():
        raise EgressError("output path must not be a symlink")
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
        raise EgressError("output could not be written") from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)


def _require_root() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise EgressError("Linux root is required")


def render(args, runner=_run) -> dict[str, object]:
    renderer = _renderer()
    try:
        values = renderer.validate_values(
            renderer.read_secret_source(None if args.stdin else args.env_file, sys.stdin)
        )
    except renderer.ConfigurationError as exc:
        raise EgressError(str(exc)) from None
    config = build_egress_config(values)
    policy = render_nftables()
    validate_nftables(policy)
    if args.apply:
        _require_root()
        if pwd is None:
            raise EgressError("Linux identity database is unavailable")
        try:
            identity = pwd.getpwnam(PROXY_USER)
        except KeyError:
            raise EgressError("proxy user is unavailable") from None
        try:
            renderer.atomic_write_json(
                EGRESS_CONFIG_PATH, config, owner=(identity.pw_uid, identity.pw_gid)
            )
        except renderer.ConfigurationError as exc:
            raise EgressError(str(exc)) from None
        _write_root_private(EGRESS_NFT_PATH, policy)
        checked = runner(["/usr/sbin/nft", "--check", "--file", str(EGRESS_NFT_PATH)])
        if checked.returncode != 0:
            raise EgressError("nftables rejected the egress policy")
    return config


def install_unit(args, runner=_run) -> list[str]:
    plan = [
        f"copy {UNIT_SOURCE.name} to {UNIT_TARGET} (root, 0644)",
        "run systemctl daemon-reload",
        "do not enable or start the service",
    ]
    if not args.apply:
        return plan
    _require_root()
    if UNIT_TARGET.is_symlink():
        raise EgressError("unit target must not be a symlink")
    _write_root_private(UNIT_TARGET, UNIT_SOURCE.read_text(encoding="utf-8"))
    os.chmod(UNIT_TARGET, 0o644)
    if runner(["/usr/bin/systemctl", "daemon-reload"]).returncode != 0:
        raise EgressError("systemd daemon-reload failed")
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram Bot API egress proxy (bot only)")
    commands = parser.add_subparsers(dest="command", required=True)
    render_parser = commands.add_parser("render")
    source = render_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--env-file", type=Path)
    source.add_argument("--stdin", action="store_true")
    render_mode = render_parser.add_mutually_exclusive_group(required=True)
    render_mode.add_argument("--check", action="store_true")
    render_mode.add_argument("--apply", action="store_true")
    render_parser.add_argument("--show-redacted", action="store_true")
    unit_parser = commands.add_parser("install-unit")
    unit_mode = unit_parser.add_mutually_exclusive_group(required=True)
    unit_mode.add_argument("--dry-run", action="store_true")
    unit_mode.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "render":
            if args.env_file is not None and args.env_file != MAXINIK_ENV_PATH:
                raise EgressError("secret source must be /etc/denstock-ai/maxinik.env")
            config = render(args)
            if args.show_redacted:
                print(json.dumps(redacted_summary(config), sort_keys=True))
            elif args.apply:
                print("wrote telegram egress configuration and firewall policy; secrets redacted")
            else:
                print("telegram egress configuration valid; secrets redacted")
        else:
            print(json.dumps(install_unit(args)))
        return 0
    except EgressError as exc:
        print(f"egress_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
