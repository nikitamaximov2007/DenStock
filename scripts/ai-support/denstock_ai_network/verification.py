import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from .config import LauncherConfigurationError, load_launcher_config
from .constants import (
    CODEX_CLI_VERSION,
    LAUNCHER_CONFIG_PATH,
    LAUNCHER_VERSION,
    PROTOCOL_VERSION,
    SING_BOX_VERSION,
)
from .installer import (
    CODEX_BINARY,
    INSTALL_PACKAGE,
    SING_BOX_BINARY,
    installed_codex_version,
    installed_sing_box_version,
    verify_installed_codex_binary,
)

LAUNCHER_BINARY = Path("/usr/local/sbin/denstock-ai-launcher")
EXPECTED_HANDSHAKE = {
    "protocol_version": PROTOCOL_VERSION,
    "launcher_version": LAUNCHER_VERSION,
    "codex_cli_version": CODEX_CLI_VERSION,
    "network_mode": "maxinik-proxy-only",
    "direct_network_blocked": True,
    "proxy_health": "ok",
}


class VerificationError(RuntimeError):
    pass


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise VerificationError(f"command failed: {Path(argv[0]).name}")
    return result


def _require_root_file(path: Path, *, executable: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise VerificationError(f"required file is unavailable: {path.name}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VerificationError(f"required file is unsafe: {path.name}")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise VerificationError(f"required file metadata is unsafe: {path.name}")
    if executable and not stat.S_IMODE(info.st_mode) & 0o100:
        raise VerificationError(f"required file is not executable: {path.name}")


def verify_installed(runner=_run) -> dict[str, object]:
    try:
        load_launcher_config(LAUNCHER_CONFIG_PATH)
    except LauncherConfigurationError as exc:
        raise VerificationError("launcher configuration is invalid") from exc
    for path, executable in (
        (CODEX_BINARY, True),
        (SING_BOX_BINARY, True),
        (LAUNCHER_BINARY, True),
        (INSTALL_PACKAGE / "launcher.py", False),
    ):
        _require_root_file(path, executable=executable)
    if installed_codex_version(runner) != CODEX_CLI_VERSION:
        raise VerificationError("Codex version is not pinned")
    try:
        verify_installed_codex_binary()
    except Exception as exc:
        raise VerificationError("Codex binary does not match the pinned artifact") from exc
    if installed_sing_box_version(runner) != SING_BOX_VERSION:
        raise VerificationError("sing-box version is not pinned")
    return {
        "installed": True,
        "codex_cli_version": CODEX_CLI_VERSION,
        "sing_box_version": SING_BOX_VERSION,
    }


def verify_runtime(runner=_run) -> dict[str, object]:
    result = verify_installed(runner)
    capabilities = runner([str(LAUNCHER_BINARY), "capabilities", "--json"], check=False)
    try:
        handshake = json.loads(capabilities.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("launcher handshake is invalid") from exc
    if capabilities.returncode != 0 or capabilities.stderr or handshake != EXPECTED_HANDSHAKE:
        raise VerificationError("launcher handshake failed closed")
    login = runner([str(LAUNCHER_BINARY), "login-status"], check=False)
    if login.returncode != 0 or login.stdout or login.stderr != b"Logged in using ChatGPT\n":
        raise VerificationError("ChatGPT login status is invalid")
    return {**result, "runtime": True, "handshake": handshake, "chatgpt_login": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the DenisStock AI host isolation layer")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--installed", action="store_true")
    mode.add_argument("--runtime", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "posix" or os.geteuid() != 0:
        print("verification_error: Linux root is required", file=sys.stderr)
        return 2
    try:
        result = verify_runtime() if args.runtime else verify_installed()
    except VerificationError as exc:
        print(f"verification_error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
