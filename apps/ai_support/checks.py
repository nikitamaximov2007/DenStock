import os
import re
import shutil
import stat
import time
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Tags, register

from .contracts import AUDITED_CODEX_CLI_VERSION, normalize_provider_name
from .providers.external_launcher import (
    ExternalLauncherError,
    query_launcher_ready,
    validate_launcher_socket,
)

VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


@register(Tags.security)
def private_media_is_not_public(app_configs, **kwargs):
    private_root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    public_root = Path(settings.MEDIA_ROOT).resolve()
    if private_root == public_root or public_root in private_root.parents:
        return [
            Error(
                "PRIVATE_MEDIA_ROOT must be outside MEDIA_ROOT.",
                hint="Use a private directory mounted only into the Django web service.",
                id="ai_support.E001",
            )
        ]
    return []


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _contains_symlink(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink() or current.is_junction():
            return True
        if current == current.parent:
            return False
        current = current.parent


def _positive_integer(value) -> bool:
    return type(value) is int and value > 0


def _is_posix_platform() -> bool:
    return os.name == "posix"


def _binary_path(binary: str) -> Path | None:
    found = shutil.which(binary)
    return Path(found).resolve() if found else None


def _posix_path_errors(home: Path, workspace: Path, launch_mode: str) -> list[Error]:
    if os.name == "nt":
        return []
    errors = []
    home_stat = home.stat()
    workspace_stat = workspace.stat()
    if stat.S_IMODE(home_stat.st_mode) & 0o077:
        errors.append(
            Error(
                "AI_SUPPORT_CODEX_HOME must not be accessible by group or others.",
                id="ai_support.E013",
            )
        )
    if stat.S_IMODE(workspace_stat.st_mode) & 0o007:
        errors.append(
            Error(
                "AI_SUPPORT_CODEX_WORKSPACE must not be accessible by others.",
                id="ai_support.E013",
            )
        )
    if launch_mode == "direct_dev" and (
        home_stat.st_uid != os.getuid() or workspace_stat.st_uid != os.getuid()
    ):
        errors.append(
            Error(
                "Direct development Codex paths must be owned by the Django user.",
                id="ai_support.E013",
            )
        )
    for parent in {home.parent, workspace.parent}:
        mode = stat.S_IMODE(parent.stat().st_mode)
        if mode & 0o002 and not mode & stat.S_ISVTX:
            errors.append(
                Error(
                    "Codex runtime parent directories must not be world-writable.",
                    id="ai_support.E013",
                )
            )
    return errors


def _external_workspace_errors(workspace: Path) -> list[Error]:
    workspace_info = workspace.stat()
    if (
        workspace != Path("/var/lib/denstock-ai/requests")
        or workspace_info.st_uid != 0
        or stat.S_IMODE(workspace_info.st_mode) != 0o1731
        or not os.access(workspace, os.W_OK | os.X_OK)
    ):
        return [
            Error(
                "External request workspace has unsafe ownership, mode, or access.",
                id="ai_support.E013",
            )
        ]
    return []


@register(Tags.security)
def codex_runtime_is_isolated(app_configs, **kwargs):
    if not settings.AI_SUPPORT_ENABLED:
        return []
    if not settings.DEBUG and not isinstance(settings.AI_SUPPORT_PROVIDER, str):
        return [
            Error(
                "Production AI support requires the audited external Codex launcher.",
                id="ai_support.E015",
            )
        ]
    try:
        provider = normalize_provider_name(settings.AI_SUPPORT_PROVIDER)
    except Exception:
        return [
            Error(
                "AI_SUPPORT_PROVIDER must be a valid provider name.",
                id="ai_support.E014",
            )
        ]
    if not settings.DEBUG and provider != "codex_cli":
        return [
            Error(
                "Production AI support requires the audited external Codex launcher.",
                id="ai_support.E015",
            )
        ]
    if provider != "codex_cli":
        return []
    errors = []
    binary = str(settings.AI_SUPPORT_CODEX_BINARY).strip()
    required_version = str(settings.AI_SUPPORT_CODEX_REQUIRED_VERSION)
    raw_home = str(settings.AI_SUPPORT_CODEX_HOME).strip()
    raw_workspace = str(settings.AI_SUPPORT_CODEX_WORKSPACE).strip()
    launch_mode = str(settings.AI_SUPPORT_CODEX_LAUNCH_MODE).strip().lower()

    if not settings.DEBUG and launch_mode != "external":
        return [
            Error(
                "Production AI support requires external launcher mode.",
                id="ai_support.E015",
            )
        ]

    if not settings.AI_SUPPORT_CODEX_MODEL:
        errors.append(Error("AI_SUPPORT_CODEX_MODEL is required.", id="ai_support.E002"))
    if launch_mode == "direct_dev" and (
        not raw_home or not Path(raw_home).is_absolute()
    ):
        errors.append(
            Error("AI_SUPPORT_CODEX_HOME must be an absolute path.", id="ai_support.E003")
        )
    if not raw_workspace or not Path(raw_workspace).is_absolute():
        errors.append(
            Error(
                "AI_SUPPORT_CODEX_WORKSPACE must be an absolute path.",
                id="ai_support.E004",
            )
        )
    if not required_version or not VERSION_PATTERN.fullmatch(required_version):
        errors.append(
            Error(
                "AI_SUPPORT_CODEX_REQUIRED_VERSION must be a pinned semantic version.",
                id="ai_support.E008",
            )
        )
    elif required_version != AUDITED_CODEX_CLI_VERSION:
        errors.append(
            Error(
                "AI_SUPPORT_CODEX_REQUIRED_VERSION must equal the audited Codex CLI version "
                f"{AUDITED_CODEX_CLI_VERSION}. Changing it requires code, tests, and a new "
                "security audit.",
                id="ai_support.E008",
            )
        )
    if settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY != 1:
        errors.append(
            Error(
                "Shared CODEX_HOME requires AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY=1.",
                id="ai_support.E010",
            )
        )
    if launch_mode == "external":
        socket_path = Path(str(settings.AI_SUPPORT_CODEX_LAUNCHER_SOCKET).strip())
        api_key_names = ("OPENAI" + "_API_KEY", "CODEX" + "_API_KEY")
        if any(os.environ.get(name) for name in api_key_names):
            errors.append(
                Error(
                    "External AI support must use ChatGPT login, not an API key.",
                    id="ai_support.E016",
                )
            )
        if settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION:
            errors.append(
                Error(
                    "Direct Codex execution must be disabled in external mode.",
                    id="ai_support.E011",
                )
            )
        if (
            not _is_posix_platform()
            or socket_path != Path("/run/denstock-ai/launcher.sock")
        ):
            errors.append(
                Error(
                    "External launcher requires the audited Linux Unix socket path.",
                    id="ai_support.E011",
                )
            )
        elif not errors:
            try:
                validate_launcher_socket(socket_path)
                query_launcher_ready(
                    socket_path,
                    deadline=time.monotonic()
                    + min(settings.AI_SUPPORT_CODEX_TIMEOUT_SECONDS, 15),
                )
            except ExternalLauncherError:
                errors.append(
                    Error(
                        "External launcher handshake or ChatGPT login check failed closed.",
                        id="ai_support.E015",
                    )
                )
    elif launch_mode == "direct_dev":
        resolved_binary = _binary_path(binary) if binary else None
        if (
            resolved_binary is None
            or not resolved_binary.is_file()
            or (os.name != "nt" and not os.access(resolved_binary, os.X_OK))
        ):
            errors.append(
                Error("AI_SUPPORT_CODEX_BINARY does not exist.", id="ai_support.E009")
            )
        if not settings.DEBUG or not settings.AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION:
            errors.append(
                Error(
                    "Direct Codex execution is allowed only by an explicit development opt-in.",
                    id="ai_support.E011",
                )
            )
        if os.name == "nt" and (resolved_binary is None or resolved_binary.suffix != ".exe"):
            errors.append(
                Error(
                    "Windows direct_dev mode requires an explicit codex.exe path.",
                    id="ai_support.E011",
                )
            )
    else:
        errors.append(
            Error(
                "AI support requires external launcher mode or explicit direct_dev mode.",
                id="ai_support.E011",
            )
        )
    limits = (
        settings.AI_SUPPORT_CODEX_TIMEOUT_SECONDS,
        settings.AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES,
        settings.AI_SUPPORT_CODEX_MAX_STDERR_BYTES,
        settings.AI_SUPPORT_CODEX_MAX_PROMPT_CHARS,
        settings.AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY,
        settings.AI_SUPPORT_CODEX_RUNTIME_RETENTION_HOURS,
    )
    if not all(_positive_integer(value) for value in limits):
        errors.append(
            Error("Codex runtime limits must be positive integers.", id="ai_support.E007")
        )

    if not raw_workspace or not Path(raw_workspace).is_absolute():
        return errors
    workspace_path = Path(raw_workspace)
    workspace = workspace_path.resolve()
    protected = {
        Path(settings.BASE_DIR).resolve(),
        Path(settings.MEDIA_ROOT).resolve(),
        Path(settings.PRIVATE_MEDIA_ROOT).resolve(),
        Path(settings.BACKUP_ROOT).resolve(),
    }
    paths = (workspace,)
    home_path = None
    if launch_mode == "direct_dev" and raw_home and Path(raw_home).is_absolute():
        home_path = Path(raw_home)
        home = home_path.resolve()
        paths = (home, workspace)
    if (home_path is not None and _overlaps(home, workspace)) or any(
        _overlaps(path, protected_path) for path in paths for protected_path in protected
    ):
        errors.append(
            Error(
                "Codex home and workspace must be isolated from DenisStock data.",
                id="ai_support.E006",
            )
        )
    if not workspace_path.is_dir() or (home_path is not None and not home_path.is_dir()):
        errors.append(
            Error(
                "Codex home and workspace must already exist as directories.",
                id="ai_support.E005",
            )
        )
        return errors
    if _contains_symlink(workspace_path) or (
        home_path is not None and _contains_symlink(home_path)
    ):
        errors.append(
            Error("Codex home and workspace must not use symlinks.", id="ai_support.E012")
        )
    if launch_mode == "direct_dev" and home_path is not None:
        errors.extend(_posix_path_errors(home, workspace, launch_mode))
    elif launch_mode == "external" and _is_posix_platform():
        errors.extend(_external_workspace_errors(workspace))
    return errors
