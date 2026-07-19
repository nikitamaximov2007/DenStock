import argparse
import base64
import binascii
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from pathlib import Path

from .constants import DEFAULT_PROXY_HOST, PROXY_USER, SING_BOX_CONFIG_PATH

try:
    import pwd
except ImportError:  # pragma: no cover - exercised only by Linux deployment
    pwd = None

REQUIRED_FIELDS = {
    "MAXINIK_SERVER",
    "MAXINIK_PORT",
    "MAXINIK_UUID",
    "MAXINIK_FLOW",
    "MAXINIK_REALITY_PUBLIC_KEY",
    "MAXINIK_REALITY_SHORT_ID",
    "MAXINIK_REALITY_SNI",
    "MAXINIK_FINGERPRINT",
    "MAXINIK_LOCAL_PROXY_HOST",
    "MAXINIK_LOCAL_PROXY_PORT",
}
OPTIONAL_FIELDS = {
    "MAXINIK_ALPN",
    "MAXINIK_PACKET_ENCODING",
    "MAXINIK_TRANSPORT_TYPE",
    "MAXINIK_TRANSPORT_PATH",
}
ALLOWED_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS
FINGERPRINTS = {
    "chrome",
    "firefox",
    "edge",
    "safari",
    "360",
    "qq",
    "ios",
    "android",
}
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
SHORT_ID = re.compile(r"(?:[0-9A-Fa-f]{2}){1,8}")
MAX_SECRET_SOURCE_BYTES = 64 * 1024


class ConfigurationError(ValueError):
    pass


def _validate_secret_source_info(info) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ConfigurationError("secret source must be a regular non-symlink file")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600:
        raise ConfigurationError("secret source mode must be 0600")
    if os.name == "posix" and info.st_uid != 0:
        raise ConfigurationError("secret source must be root-owned")


def _read_private_file(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ConfigurationError("secret source is unavailable") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ConfigurationError("secret source must be a regular non-symlink file")
    _validate_secret_source_info(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            _validate_secret_source_info(opened)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ConfigurationError("secret source changed while opening")
            with os.fdopen(descriptor, "rb", closefd=False) as file:
                encoded = file.read(MAX_SECRET_SOURCE_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ConfigurationError("secret source cannot be read safely") from exc
    if len(encoded) > MAX_SECRET_SOURCE_BYTES:
        raise ConfigurationError("secret source is too large")
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("secret source is not UTF-8") from exc


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"invalid assignment on line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in ALLOWED_FIELDS:
            raise ConfigurationError(f"unsupported setting: {key}")
        if key in values:
            raise ConfigurationError(f"duplicate setting: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ConfigurationError(f"invalid control character in {key}")
        values[key] = value
    return values


def read_secret_source(path: Path | None, stream) -> dict[str, str]:
    if path is not None:
        return parse_env(_read_private_file(path))
    text = stream.read(MAX_SECRET_SOURCE_BYTES + 1)
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ConfigurationError("secret source is not UTF-8") from exc
    if encoded_size > MAX_SECRET_SOURCE_BYTES:
        raise ConfigurationError("secret source is too large")
    return parse_env(text)


def _valid_host(value: str, *, allow_ip: bool) -> bool:
    if allow_ip:
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            pass
    if len(value) > 253 or not value or value.endswith("."):
        return False
    return all(HOST_LABEL.fullmatch(label) for label in value.split("."))


def _valid_public_key(value: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        return False
    try:
        return len(base64.urlsafe_b64decode(value + "=")) == 32
    except (ValueError, binascii.Error):
        return False


def validate_values(raw_values: dict[str, str]) -> dict[str, object]:
    missing = sorted(field for field in REQUIRED_FIELDS if not raw_values.get(field))
    if missing:
        raise ConfigurationError(f"missing required setting: {missing[0]}")

    try:
        server_port = int(raw_values["MAXINIK_PORT"])
        proxy_port = int(raw_values["MAXINIK_LOCAL_PROXY_PORT"])
    except ValueError as exc:
        raise ConfigurationError("ports must be decimal integers") from exc
    if not 1 <= server_port <= 65535 or not 1024 <= proxy_port <= 65535:
        raise ConfigurationError("port is outside the allowed range")
    try:
        parsed_uuid = uuid.UUID(raw_values["MAXINIK_UUID"])
    except ValueError as exc:
        raise ConfigurationError("MAXINIK_UUID is invalid") from exc
    if str(parsed_uuid) != raw_values["MAXINIK_UUID"].lower():
        raise ConfigurationError("MAXINIK_UUID must use canonical form")
    if raw_values["MAXINIK_FLOW"] != "xtls-rprx-vision":
        raise ConfigurationError("MAXINIK_FLOW must be xtls-rprx-vision")
    if not _valid_public_key(raw_values["MAXINIK_REALITY_PUBLIC_KEY"]):
        raise ConfigurationError("MAXINIK_REALITY_PUBLIC_KEY is invalid")
    if not SHORT_ID.fullmatch(raw_values["MAXINIK_REALITY_SHORT_ID"]):
        raise ConfigurationError("MAXINIK_REALITY_SHORT_ID is invalid")
    if not _valid_host(raw_values["MAXINIK_SERVER"], allow_ip=True):
        raise ConfigurationError("MAXINIK_SERVER is invalid")
    if not _valid_host(raw_values["MAXINIK_REALITY_SNI"], allow_ip=False):
        raise ConfigurationError("MAXINIK_REALITY_SNI is invalid")
    fingerprint = raw_values["MAXINIK_FINGERPRINT"].lower()
    if fingerprint not in FINGERPRINTS:
        raise ConfigurationError("MAXINIK_FINGERPRINT is unsupported")
    if raw_values["MAXINIK_LOCAL_PROXY_HOST"] != DEFAULT_PROXY_HOST:
        raise ConfigurationError("local proxy must bind only to 127.0.0.1")

    packet_encoding = raw_values.get("MAXINIK_PACKET_ENCODING", "xudp")
    if packet_encoding not in {"", "packetaddr", "xudp"}:
        raise ConfigurationError("MAXINIK_PACKET_ENCODING is unsupported")
    transport_type = raw_values.get("MAXINIK_TRANSPORT_TYPE", "tcp")
    if transport_type not in {"", "tcp"}:
        raise ConfigurationError("only direct TCP transport is supported")
    if raw_values.get("MAXINIK_TRANSPORT_PATH", ""):
        raise ConfigurationError("MAXINIK_TRANSPORT_PATH is not valid for direct TCP")
    alpn = [item.strip() for item in raw_values.get("MAXINIK_ALPN", "").split(",") if item.strip()]
    if any(item not in {"h2", "http/1.1"} for item in alpn):
        raise ConfigurationError("MAXINIK_ALPN contains an unsupported value")

    return {
        **raw_values,
        "MAXINIK_PORT": server_port,
        "MAXINIK_LOCAL_PROXY_PORT": proxy_port,
        "MAXINIK_FINGERPRINT": fingerprint,
        "MAXINIK_PACKET_ENCODING": packet_encoding,
        "MAXINIK_ALPN": alpn,
    }


def build_sing_box_config(values: dict[str, object]) -> dict[str, object]:
    tls: dict[str, object] = {
        "enabled": True,
        "server_name": values["MAXINIK_REALITY_SNI"],
        "utls": {"enabled": True, "fingerprint": values["MAXINIK_FINGERPRINT"]},
        "reality": {
            "enabled": True,
            "public_key": values["MAXINIK_REALITY_PUBLIC_KEY"],
            "short_id": values["MAXINIK_REALITY_SHORT_ID"],
        },
    }
    if values["MAXINIK_ALPN"]:
        tls["alpn"] = values["MAXINIK_ALPN"]
    outbound: dict[str, object] = {
        "type": "vless",
        "tag": "maxinik-vless",
        "server": values["MAXINIK_SERVER"],
        "server_port": values["MAXINIK_PORT"],
        "uuid": values["MAXINIK_UUID"],
        "flow": values["MAXINIK_FLOW"],
        "network": "tcp",
        "tls": tls,
    }
    if values["MAXINIK_PACKET_ENCODING"]:
        outbound["packet_encoding"] = values["MAXINIK_PACKET_ENCODING"]
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "local-mixed",
                "listen": DEFAULT_PROXY_HOST,
                "listen_port": values["MAXINIK_LOCAL_PROXY_PORT"],
                "set_system_proxy": False,
            }
        ],
        "outbounds": [outbound],
        "route": {"final": "maxinik-vless", "auto_detect_interface": True},
    }


def atomic_write_json(
    path: Path, payload: dict[str, object], *, owner: tuple[int, int] | None = None
) -> None:
    if path.exists() and path.is_symlink():
        raise ConfigurationError("output path must not be a symlink")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ConfigurationError("output parent must be a non-symlink directory")
    encoded = (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as file:
            temporary_name = file.name
            os.chmod(temporary_name, 0o600)
            if owner is not None:
                os.chown(temporary_name, *owner)
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
        if owner is not None:
            os.chown(path, *owner)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise ConfigurationError("output configuration could not be written") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def redacted_summary(config: dict[str, object]) -> dict[str, object]:
    inbound = config["inbounds"][0]
    return {
        "client": "sing-box",
        "network": "VLESS+Reality",
        "proxy_bind": f"{inbound['listen']}:{inbound['listen_port']}",
        "proxy_protocol": "mixed-http-socks",
        "system_proxy": False,
        "secrets": "redacted",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render root-only MAXINIK sing-box config")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--env-file", type=Path)
    source.add_argument("--stdin", action="store_true")
    parser.add_argument("--output", type=Path, default=SING_BOX_CONFIG_PATH)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-redacted", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.check and not args.dry_run and args.output != SING_BOX_CONFIG_PATH:
            raise ConfigurationError("final output path must be /etc/denstock-ai/sing-box.json")
        raw_values = read_secret_source(args.env_file, sys.stdin)
        config = build_sing_box_config(validate_values(raw_values))
        if not args.check and not args.dry_run:
            owner = None
            if os.name == "posix":
                if os.geteuid() != 0:
                    raise ConfigurationError("rendering the final config requires root")
                assert pwd is not None
                try:
                    identity = pwd.getpwnam(PROXY_USER)
                except KeyError as exc:
                    raise ConfigurationError("proxy user is unavailable") from exc
                owner = (identity.pw_uid, identity.pw_gid)
            atomic_write_json(args.output, config, owner=owner)
        if args.show_redacted:
            print(json.dumps(redacted_summary(config), sort_keys=True))
        elif args.check:
            print("configuration valid; secrets redacted")
        elif args.dry_run:
            print(f"would write root-only configuration to {args.output}; secrets redacted")
        else:
            print(f"wrote root-only configuration to {args.output}; secrets redacted")
        return 0
    except ConfigurationError as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
