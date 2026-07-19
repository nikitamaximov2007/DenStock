import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from .config import MODEL_PATTERN
from .constants import (
    AI_USER,
    CLIENT_GROUP,
    CODEX_ARCHIVE_MEMBER,
    CODEX_ARCHIVE_NAME,
    CODEX_ARCHIVE_SHA256,
    CODEX_ARCHIVE_URL,
    CODEX_BINARY_BYTES,
    CODEX_BINARY_SHA256,
    CODEX_CLI_VERSION,
    CONFIG_ROOT,
    DEFAULT_PROXY_HOST,
    DEFAULT_PROXY_PORT,
    LAUNCHER_CONFIG_PATH,
    PROXY_USER,
    REQUEST_ROOT_MODE,
    SING_BOX_DEB_NAME,
    SING_BOX_DEB_SHA256,
    SING_BOX_DEB_URL,
    SING_BOX_VERSION,
)

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - exercised only by Linux deployment
    grp = None
    pwd = None

INSTALL_ROOT = Path("/usr/local/lib/denstock-ai")
INSTALL_PACKAGE = INSTALL_ROOT / "denstock_ai_network"
INSTALL_BIN = INSTALL_ROOT / "bin"
SING_BOX_BINARY = INSTALL_BIN / "sing-box"
CODEX_BINARY = Path("/usr/local/bin/codex")
CODEX_MARKER = INSTALL_ROOT / "codex.sha256"
SYSTEMD_ROOT = Path("/etc/systemd/system")
TMPFILES_ROOT = Path("/etc/tmpfiles.d")
DOC_ROOT = Path("/usr/local/share/doc/denstock-ai")
STATE_ROOT = Path("/var/lib/denstock-ai")
CODEX_HOME = STATE_ROOT / "codex-home"
RUNTIME_ROOT = STATE_ROOT / "requests"
LOCK_ROOT = Path("/run/denstock-ai/locks")

PACKAGE_FILES = (
    "__init__.py",
    "config.py",
    "constants.py",
    "firewall.py",
    "health.py",
    "installer.py",
    "launcher.py",
    "protocol.py",
    "renderer.py",
    "rollback.py",
    "verification.py",
)
UNIT_FILES = (
    "denstock-ai-proxy.service",
    "denstock-ai-firewall.service",
    "denstock-ai-launcher.socket",
    "denstock-ai-launcher@.service",
)
WRAPPERS = (
    "denstock-ai-install",
    "denstock-ai-launcher",
    "denstock-ai-render-maxinik",
    "denstock-ai-rollback",
    "denstock-ai-update",
    "denstock-ai-verify",
)
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_CODEX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_CODEX_BINARY_BYTES = 320 * 1024 * 1024


class InstallationError(RuntimeError):
    pass


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sing_box_package(path: Path) -> None:
    if path.name != SING_BOX_DEB_NAME or sha256_file(path) != SING_BOX_DEB_SHA256:
        raise InstallationError("sing-box package checksum mismatch")


def verify_codex_archive(path: Path) -> None:
    if path.name != CODEX_ARCHIVE_NAME or sha256_file(path) != CODEX_ARCHIVE_SHA256:
        raise InstallationError("Codex archive checksum mismatch")


def verify_installed_codex_binary(path: Path = CODEX_BINARY) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallationError("Codex binary is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size != CODEX_BINARY_BYTES
        or info.st_nlink != 1
        or sha256_file(path) != CODEX_BINARY_SHA256
    ):
        raise InstallationError("Codex binary does not match the pinned artifact")
    if os.name == "posix" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
        raise InstallationError("Codex binary metadata is unsafe")


def verify_codex_install_marker(path: Path = CODEX_MARKER) -> None:
    try:
        info = path.lstat()
        content = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise InstallationError("Codex install marker is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or content != f"{CODEX_BINARY_SHA256}\n"
        or os.name == "posix"
        and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o644)
    ):
        raise InstallationError("Codex install marker is unsafe")


def _write_codex_install_marker() -> None:
    descriptor = os.open(CODEX_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as file:
        file.write(f"{CODEX_BINARY_SHA256}\n".encode("ascii"))
        file.flush()
        os.fsync(file.fileno())
    os.chown(CODEX_MARKER, 0, 0)
    os.chmod(CODEX_MARKER, 0o644)


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise InstallationError(f"command failed: {Path(argv[0]).name}")
    return result


def _require_units_inactive(runner=_run) -> None:
    for unit in UNIT_FILES:
        if not unit.endswith((".service", ".socket")) or "@" in unit:
            continue
        result = runner(["/usr/bin/systemctl", "is-active", "--quiet", unit], check=False)
        if result.returncode == 0:
            raise InstallationError(f"refusing to update active unit: {unit}")
    launcher_instances = "denstock-ai-launcher@*.service"
    result = runner(
        ["/usr/bin/systemctl", "is-active", "--quiet", launcher_instances],
        check=False,
    )
    if result.returncode == 0:
        raise InstallationError("refusing to update an active launcher instance")


def installed_sing_box_version(runner=_run) -> str:
    try:
        result = runner([str(SING_BOX_BINARY), "version"], check=False)
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    match = re.search(rb"sing-box version ([0-9]+\.[0-9]+\.[0-9]+)(?:\r?\n|\s)", result.stdout)
    return match.group(1).decode("ascii") if match else ""


def installed_codex_version(runner=_run) -> str:
    try:
        result = runner([str(CODEX_BINARY), "--version"], check=False)
    except OSError:
        return ""
    if result.returncode != 0 or result.stderr:
        return ""
    expected = f"codex-cli {CODEX_CLI_VERSION}\n".encode("ascii")
    return CODEX_CLI_VERSION if result.stdout == expected else ""


def _download(url: str, destination: Path, *, max_bytes: int, label: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=90) as response:
            with destination.open("xb") as file:
                os.chmod(destination, 0o600)
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise InstallationError(f"{label} exceeds the size limit")
                    file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
    except OSError as exc:
        raise InstallationError(f"{label} download failed") from exc


def extract_codex_binary(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, mode="r:gz") as source:
            members = source.getmembers()
            if len(members) != 1:
                raise InstallationError("Codex archive must contain exactly one member")
            member = members[0]
            if (
                member.name != CODEX_ARCHIVE_MEMBER
                or not member.isreg()
                or member.size <= 0
                or member.size > MAX_CODEX_BINARY_BYTES
            ):
                raise InstallationError("Codex archive contains an unsafe member")
            extracted = source.extractfile(member)
            if extracted is None:
                raise InstallationError("Codex binary could not be extracted")
            with extracted, destination.open("xb") as output:
                os.chmod(destination, 0o600)
                copied = 0
                while chunk := extracted.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > member.size or copied > MAX_CODEX_BINARY_BYTES:
                        raise InstallationError("Codex binary exceeds the size limit")
                    output.write(chunk)
                if copied != member.size:
                    raise InstallationError("Codex binary size does not match the archive")
                output.flush()
                os.fsync(output.fileno())
    except (OSError, tarfile.TarError) as exc:
        raise InstallationError("Codex archive could not be extracted") from exc


def _download_and_install_codex(runner=_run) -> None:
    binary_present = CODEX_BINARY.exists() or CODEX_BINARY.is_symlink()
    marker_present = CODEX_MARKER.exists() or CODEX_MARKER.is_symlink()
    if binary_present or marker_present:
        if not binary_present or not marker_present:
            raise InstallationError("refusing to manage an incomplete Codex installation")
        verify_installed_codex_binary(CODEX_BINARY)
        verify_codex_install_marker(CODEX_MARKER)
        if installed_codex_version(runner) != CODEX_CLI_VERSION:
            raise InstallationError("installed Codex version is not pinned")
        return
    temporary_directory = Path(tempfile.mkdtemp(prefix="denstock-ai-codex-"))
    try:
        os.chmod(temporary_directory, 0o700)
        archive = temporary_directory / CODEX_ARCHIVE_NAME
        _download(
            CODEX_ARCHIVE_URL,
            archive,
            max_bytes=MAX_CODEX_ARCHIVE_BYTES,
            label="Codex archive",
        )
        verify_codex_archive(archive)
        extracted_binary = temporary_directory / CODEX_ARCHIVE_MEMBER
        extract_codex_binary(archive, extracted_binary)
        if (
            extracted_binary.stat().st_size != CODEX_BINARY_BYTES
            or sha256_file(extracted_binary) != CODEX_BINARY_SHA256
        ):
            raise InstallationError("Codex binary does not match the pinned artifact")
        _copy(extracted_binary, CODEX_BINARY, 0o755)
        verify_installed_codex_binary(CODEX_BINARY)
        if installed_codex_version(runner) != CODEX_CLI_VERSION:
            CODEX_BINARY.unlink(missing_ok=True)
            raise InstallationError("installed Codex version is not pinned")
        try:
            _write_codex_install_marker()
        except OSError as exc:
            CODEX_BINARY.unlink(missing_ok=True)
            raise InstallationError("Codex install marker could not be written") from exc
    finally:
        shutil.rmtree(temporary_directory)


def _download_and_install_sing_box(runner=_run) -> None:
    if installed_sing_box_version(runner) == SING_BOX_VERSION:
        return
    temporary_directory = Path(tempfile.mkdtemp(prefix="denstock-ai-sing-box-"))
    try:
        os.chmod(temporary_directory, 0o700)
        package = temporary_directory / SING_BOX_DEB_NAME
        _download(
            SING_BOX_DEB_URL,
            package,
            max_bytes=MAX_PACKAGE_BYTES,
            label="sing-box package",
        )
        verify_sing_box_package(package)
        extracted = temporary_directory / "extracted"
        extracted.mkdir(mode=0o700)
        runner(["/usr/bin/dpkg-deb", "--extract", str(package), str(extracted)])
        extracted_binary = extracted / "usr" / "bin" / "sing-box"
        if not extracted_binary.is_file() or extracted_binary.is_symlink():
            raise InstallationError("verified package does not contain a regular sing-box binary")
        _copy(extracted_binary, SING_BOX_BINARY, 0o755)
        if installed_sing_box_version(runner) != SING_BOX_VERSION:
            raise InstallationError("installed sing-box version is not pinned")
    finally:
        shutil.rmtree(temporary_directory)


def _ensure_group(name: str, runner=_run) -> None:
    assert grp is not None
    try:
        grp.getgrnam(name)
    except KeyError:
        runner(["/usr/sbin/groupadd", "--system", name])


def _ensure_user(name: str, home: Path, runner=_run) -> None:
    assert grp is not None and pwd is not None
    try:
        identity = pwd.getpwnam(name)
    except KeyError:
        runner(
            [
                "/usr/sbin/useradd",
                "--system",
                "--user-group",
                "--home-dir",
                str(home),
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                name,
            ]
        )
        identity = pwd.getpwnam(name)
    if identity.pw_shell != "/usr/sbin/nologin" or Path(identity.pw_dir) != home:
        raise InstallationError(f"existing user {name} has incompatible account settings")
    groups = {group.gr_name for group in grp.getgrall() if name in group.gr_mem}
    forbidden_groups = {"docker", CLIENT_GROUP}
    primary_group = grp.getgrgid(identity.pw_gid).gr_name
    if groups & forbidden_groups or primary_group in forbidden_groups:
        raise InstallationError(f"existing user {name} has a forbidden group")
    if (home / ".ssh").exists():
        raise InstallationError(f"existing user {name} has an SSH directory")


def _ensure_directory(path: Path, mode: int, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallationError(f"unsafe directory: {path}")
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def _copy(source: Path, destination: Path, mode: int) -> None:
    if not source.is_file() or source.is_symlink():
        raise InstallationError(f"missing deployment source: {source.name}")
    if destination.exists() and destination.is_symlink():
        raise InstallationError(f"unsafe install target: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.new")
    temporary.unlink(missing_ok=True)
    shutil.copyfile(source, temporary)
    os.chown(temporary, 0, 0)
    os.chmod(temporary, mode)
    os.replace(temporary, destination)


def launcher_payload(model: str, request_creator_uid: int, proxy_port: int) -> dict[str, object]:
    if not MODEL_PATTERN.fullmatch(model):
        raise InstallationError("model is invalid")
    if request_creator_uid <= 0 or not 1024 <= proxy_port <= 65535:
        raise InstallationError("request creator UID or proxy port is invalid")
    return {
        "protocol_version": 1,
        "codex_binary": "/usr/local/bin/codex",
        "codex_cli_version": CODEX_CLI_VERSION,
        "model": model,
        "codex_home": str(CODEX_HOME),
        "runtime_root": str(RUNTIME_ROOT),
        "lock_root": str(LOCK_ROOT),
        "ai_user": AI_USER,
        "request_creator_uid": request_creator_uid,
        "proxy_host": DEFAULT_PROXY_HOST,
        "proxy_port": proxy_port,
        "timeout_seconds": 60,
        "max_prompt_bytes": 64 * 1024,
        "max_stdout_bytes": 65536,
        "max_stderr_bytes": 16384,
        "max_image_bytes": 10 * 1024 * 1024,
        "proxy_service": "denstock-ai-proxy.service",
        "firewall_table": "denstock_ai",
    }


def _write_launcher_config(payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if LAUNCHER_CONFIG_PATH.exists():
        info = LAUNCHER_CONFIG_PATH.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or os.name == "posix"
            and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600)
        ):
            raise InstallationError("launcher config target has unsafe metadata")
        if LAUNCHER_CONFIG_PATH.read_bytes() != encoded:
            raise InstallationError("existing launcher config differs; refusing to overwrite")
        return
    descriptor = os.open(LAUNCHER_CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(encoded)
        file.flush()
        os.fsync(file.fileno())
    os.chown(LAUNCHER_CONFIG_PATH, 0, 0)
    os.chmod(LAUNCHER_CONFIG_PATH, 0o600)


def install(model: str, request_creator_uid: int, proxy_port: int, runner=_run) -> None:
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise InstallationError("the pinned package supports only amd64")
    if grp is None or pwd is None:
        raise InstallationError("Linux identity database is unavailable")
    _require_units_inactive(runner)
    _ensure_group(CLIENT_GROUP, runner)
    _ensure_user(AI_USER, CODEX_HOME, runner)
    _ensure_user(PROXY_USER, Path("/var/lib/denstock-ai-proxy"), runner)
    ai = pwd.getpwnam(AI_USER)
    proxy = pwd.getpwnam(PROXY_USER)
    client_group = grp.getgrnam(CLIENT_GROUP)

    _ensure_directory(CONFIG_ROOT, 0o750, 0, proxy.pw_gid)
    _ensure_directory(STATE_ROOT, 0o711, 0, 0)
    _ensure_directory(CODEX_HOME, 0o700, ai.pw_uid, ai.pw_gid)
    _ensure_directory(RUNTIME_ROOT, REQUEST_ROOT_MODE, 0, client_group.gr_gid)
    _ensure_directory(LOCK_ROOT.parent, 0o750, 0, client_group.gr_gid)
    _ensure_directory(LOCK_ROOT, 0o700, 0, 0)
    _ensure_directory(INSTALL_ROOT, 0o755, 0, 0)
    _ensure_directory(INSTALL_PACKAGE, 0o755, 0, 0)
    _ensure_directory(INSTALL_BIN, 0o755, 0, 0)
    _ensure_directory(DOC_ROOT, 0o755, 0, 0)
    _download_and_install_sing_box(runner)
    _download_and_install_codex(runner)

    root = repository_root()
    package_source = root / "scripts" / "ai-support" / "denstock_ai_network"
    deploy_source = root / "deploy" / "ai-support"
    for name in PACKAGE_FILES:
        _copy(package_source / name, INSTALL_PACKAGE / name, 0o644)
    for name in UNIT_FILES:
        _copy(deploy_source / "systemd" / name, SYSTEMD_ROOT / name, 0o644)
    for name in WRAPPERS:
        _copy(deploy_source / "bin" / name, Path("/usr/local/sbin") / name, 0o755)
    _copy(
        deploy_source / "tmpfiles.d" / "denstock-ai.conf",
        TMPFILES_ROOT / "denstock-ai.conf",
        0o644,
    )
    _copy(
        deploy_source / "maxinik.env.example",
        CONFIG_ROOT / "maxinik.env.example",
        0o600,
    )
    _copy(
        root / "docs" / "operations" / "ai-support-maxinik-network.md",
        DOC_ROOT / "ai-support-maxinik-network.md",
        0o644,
    )
    _write_launcher_config(launcher_payload(model, request_creator_uid, proxy_port))
    runner(["/usr/bin/systemd-tmpfiles", "--create", "/etc/tmpfiles.d/denstock-ai.conf"])
    runner(["/usr/bin/systemctl", "daemon-reload"])


def dry_run_plan(model: str, request_creator_uid: int, proxy_port: int) -> list[str]:
    launcher_payload(model, request_creator_uid, proxy_port)
    return [
        f"verify/download official sing-box {SING_BOX_VERSION} DEB and SHA-256",
        "extract only the verified sing-box binary without package maintainer scripts",
        f"verify/download official native Codex CLI {CODEX_CLI_VERSION} archive and SHA-256",
        "extract exactly one verified Codex binary and require its exact version output",
        f"ensure system users {AI_USER}, {PROXY_USER} and group {CLIENT_GROUP}",
        "create isolated root-owned config, state, request and lock directories",
        "install root-owned launcher code, wrappers, systemd units and tmpfiles rule",
        "write launcher.json only if absent or byte-identical",
        "run systemd-tmpfiles and systemctl daemon-reload",
        "do not enable or start any service",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install the DenisStock AI host isolation layer")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--request-creator-uid", required=True, type=int)
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dry_run:
            print(json.dumps(dry_run_plan(args.model, args.request_creator_uid, args.proxy_port)))
            return 0
        if os.name != "posix" or os.geteuid() != 0:
            raise InstallationError("Linux root is required")
        install(args.model, args.request_creator_uid, args.proxy_port)
        print("installation files prepared; installer did not enable or start services")
        return 0
    except InstallationError as exc:
        print(f"installation_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
