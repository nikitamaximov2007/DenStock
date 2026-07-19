import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    AI_USER,
    CODEX_CLI_VERSION,
    DEFAULT_PROXY_HOST,
    FIREWALL_TABLE,
)

MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
EXPECTED_KEYS = {
    "protocol_version",
    "codex_binary",
    "codex_cli_version",
    "model",
    "codex_home",
    "runtime_root",
    "lock_root",
    "ai_user",
    "request_creator_uid",
    "proxy_host",
    "proxy_port",
    "timeout_seconds",
    "max_prompt_bytes",
    "max_stdout_bytes",
    "max_stderr_bytes",
    "max_image_bytes",
    "proxy_service",
    "firewall_table",
}


class LauncherConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class LauncherConfig:
    protocol_version: int
    codex_binary: Path
    codex_cli_version: str
    model: str
    codex_home: Path
    runtime_root: Path
    lock_root: Path
    ai_user: str
    request_creator_uid: int
    proxy_host: str
    proxy_port: int
    timeout_seconds: int
    max_prompt_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_image_bytes: int
    proxy_service: str
    firewall_table: str


def _absolute_under(path: Path, parent: Path) -> bool:
    return path.is_absolute() and path != parent and path.is_relative_to(parent)


def _validated_path(path: Path, *, kind: str, require_exists: bool) -> None:
    if require_exists:
        try:
            info = path.lstat()
        except OSError as exc:
            raise LauncherConfigurationError(f"{kind} is unavailable") from exc
        if stat.S_ISLNK(info.st_mode):
            raise LauncherConfigurationError(f"{kind} must not be a symlink")


def validate_launcher_config(
    payload: dict[str, object], *, require_paths: bool = True
) -> LauncherConfig:
    if set(payload) != EXPECTED_KEYS:
        raise LauncherConfigurationError("launcher config has unexpected or missing keys")
    try:
        config = LauncherConfig(
            protocol_version=int(payload["protocol_version"]),
            codex_binary=Path(str(payload["codex_binary"])),
            codex_cli_version=str(payload["codex_cli_version"]),
            model=str(payload["model"]),
            codex_home=Path(str(payload["codex_home"])),
            runtime_root=Path(str(payload["runtime_root"])),
            lock_root=Path(str(payload["lock_root"])),
            ai_user=str(payload["ai_user"]),
            request_creator_uid=int(payload["request_creator_uid"]),
            proxy_host=str(payload["proxy_host"]),
            proxy_port=int(payload["proxy_port"]),
            timeout_seconds=int(payload["timeout_seconds"]),
            max_prompt_bytes=int(payload["max_prompt_bytes"]),
            max_stdout_bytes=int(payload["max_stdout_bytes"]),
            max_stderr_bytes=int(payload["max_stderr_bytes"]),
            max_image_bytes=int(payload["max_image_bytes"]),
            proxy_service=str(payload["proxy_service"]),
            firewall_table=str(payload["firewall_table"]),
        )
    except (TypeError, ValueError) as exc:
        raise LauncherConfigurationError("launcher config contains an invalid value") from exc

    if config.protocol_version != 1:
        raise LauncherConfigurationError("unsupported protocol version")
    if config.codex_binary != Path("/usr/local/bin/codex"):
        raise LauncherConfigurationError("codex binary path is not the audited path")
    if config.codex_cli_version != CODEX_CLI_VERSION:
        raise LauncherConfigurationError("codex version is not the audited version")
    if not MODEL_PATTERN.fullmatch(config.model):
        raise LauncherConfigurationError("model is invalid")
    state_root = Path("/var/lib/denstock-ai")
    if not _absolute_under(config.codex_home, state_root):
        raise LauncherConfigurationError("CODEX_HOME must be isolated under /var/lib/denstock-ai")
    if not _absolute_under(config.runtime_root, state_root):
        raise LauncherConfigurationError("runtime root must be isolated under /var/lib/denstock-ai")
    if not _absolute_under(config.lock_root, Path("/run/denstock-ai")):
        raise LauncherConfigurationError("lock root must be isolated under /run/denstock-ai")
    if len({config.codex_home, config.runtime_root, config.lock_root}) != 3:
        raise LauncherConfigurationError("launcher paths must be distinct")
    if config.ai_user != AI_USER or config.request_creator_uid <= 0:
        raise LauncherConfigurationError("launcher identities are invalid")
    if config.proxy_host != DEFAULT_PROXY_HOST or not 1024 <= config.proxy_port <= 65535:
        raise LauncherConfigurationError("proxy must use a non-privileged 127.0.0.1 port")
    if config.proxy_service != "denstock-ai-proxy.service":
        raise LauncherConfigurationError("proxy service name is invalid")
    if config.firewall_table != FIREWALL_TABLE:
        raise LauncherConfigurationError("firewall table name is invalid")
    if not 1 <= config.timeout_seconds <= 300:
        raise LauncherConfigurationError("timeout is outside the allowed range")
    if not 1 <= config.max_prompt_bytes <= 64 * 1024:
        raise LauncherConfigurationError("prompt limit is outside the allowed range")
    if not 1 <= config.max_stdout_bytes <= 1024 * 1024:
        raise LauncherConfigurationError("stdout limit is outside the allowed range")
    if not 1 <= config.max_stderr_bytes <= 256 * 1024:
        raise LauncherConfigurationError("stderr limit is outside the allowed range")
    if not 1 <= config.max_image_bytes <= 20 * 1024 * 1024:
        raise LauncherConfigurationError("image limit is outside the allowed range")
    for path, kind in (
        (config.codex_binary, "codex binary"),
        (config.codex_home, "CODEX_HOME"),
        (config.runtime_root, "runtime root"),
        (config.lock_root, "lock root"),
    ):
        _validated_path(path, kind=kind, require_exists=require_paths)
    return config


def load_launcher_config(path: Path, *, require_paths: bool = True) -> LauncherConfig:
    try:
        info = path.lstat()
    except OSError as exc:
        raise LauncherConfigurationError("launcher config is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LauncherConfigurationError("launcher config must be a regular non-symlink file")
    if os.name == "posix":
        if info.st_uid != 0:
            raise LauncherConfigurationError("launcher config must be root-owned")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise LauncherConfigurationError("launcher config mode must be 0600")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherConfigurationError("launcher config is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise LauncherConfigurationError("launcher config must be a JSON object")
    return validate_launcher_config(payload, require_paths=require_paths)
