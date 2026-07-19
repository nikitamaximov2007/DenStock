import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import LauncherConfig, LauncherConfigurationError, load_launcher_config
from .constants import (
    CODEX_CLI_VERSION,
    CODEX_CONFIG_OVERRIDES,
    LAUNCHER_CONFIG_PATH,
    LAUNCHER_VERSION,
    PROTOCOL_VERSION,
    REQUEST_ROOT_MODE,
)
from .health import HealthResult, check_health
from .protocol import ProtocolError, decode_frame, encode_frame, response_payload, validate_request

try:
    import pwd
except ImportError:  # pragma: no cover - exercised only by Linux deployment
    pwd = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows exercises the exclusive-file fallback
    fcntl = None

VERSION_OUTPUT = f"codex-cli {CODEX_CLI_VERSION}\n".encode()
CHATGPT_AUTH_OUTPUTS = {
    b"Logged in using ChatGPT\n",
    b"Logged in using ChatGPT\r\n",
}
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "maxLength": 16000}},
    "required": ["answer"],
    "additionalProperties": False,
}
IMAGE_NAMES = {"attachment.png", "attachment.jpg", "attachment.jpeg", "attachment.webp"}


class LauncherError(RuntimeError):
    def __init__(self, code: str, returncode: int = 70):
        super().__init__(code)
        self.code = code
        self.returncode = returncode


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    stdout: bytes
    stderr: bytes
    error: str = ""


@dataclass(frozen=True)
class ProcessLimits:
    timeout_seconds: int
    max_stdout_bytes: int
    max_stderr_bytes: int


@dataclass(frozen=True)
class PreparedRequest:
    request_id: str
    directory: Path
    schema: Path
    image: Path | None
    directory_inode: int


def config_args() -> list[str]:
    return [value for override in CODEX_CONFIG_OVERRIDES for value in ("-c", override)]


def minimal_environment(config: LauncherConfig) -> dict[str, str]:
    proxy_url = f"http://{config.proxy_host}:{config.proxy_port}"
    no_proxy = "127.0.0.1,localhost"
    return {
        "CODEX_HOME": str(config.codex_home),
        "HOME": str(config.codex_home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "NO_PROXY": no_proxy,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "no_proxy": no_proxy,
    }


def version_argv(config: LauncherConfig) -> list[str]:
    return [str(config.codex_binary), "--version"]


def login_status_argv(config: LauncherConfig) -> list[str]:
    return [str(config.codex_binary), *config_args(), "login", "status"]


def exec_argv(config: LauncherConfig, request: PreparedRequest) -> list[str]:
    argv = [
        str(config.codex_binary),
        *config_args(),
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "-c",
        'approval_policy="never"',
        "--strict-config",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-schema",
        str(request.schema),
        "--model",
        config.model,
        "--cd",
        str(request.directory),
    ]
    if request.image is not None:
        argv.extend(("--image", str(request.image)))
    argv.append("-")
    return argv


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_bounded_process(
    argv: list[str],
    *,
    input_data: bytes,
    cwd: Path,
    environment: dict[str, str],
    limits: ProcessLimits,
    uid: int,
    gid: int,
) -> ProcessOutcome:
    if os.name != "posix":
        return ProcessOutcome(70, b"", b"", "unsupported_platform")
    deadline = time.monotonic() + limits.timeout_seconds
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=environment,
            shell=False,
            close_fds=True,
            start_new_session=True,
            user=uid,
            group=gid,
            extra_groups=[],
            umask=0o077,
        )
    except OSError:
        return ProcessOutcome(70, b"", b"", "spawn_failed")

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    streams = (process.stdin, process.stdout, process.stderr)
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    pending = memoryview(input_data)
    if pending:
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    limits_by_stream = {
        "stdout": limits.max_stdout_bytes,
        "stderr": limits.max_stderr_bytes,
    }
    error = ""
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                if key.data == "stdin":
                    try:
                        written = os.write(stream.fileno(), pending[:16384])
                        pending = pending[written:]
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError):
                        pending = pending[len(pending) :]
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 16384)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                target = output[key.data]
                if len(target) + len(chunk) > limits_by_stream[key.data]:
                    error = f"{key.data}_limit"
                    break
                target.extend(chunk)
            if error:
                break
            registered_streams = (entry.fileobj for entry in selector.get_map().values())
            if process.poll() is not None and process.stdin in registered_streams:
                try:
                    selector.unregister(process.stdin)
                    process.stdin.close()
                except (KeyError, OSError):
                    pass
        if error:
            _kill_process_group(process)
            return ProcessOutcome(70, bytes(output["stdout"]), bytes(output["stderr"]), error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(process)
            return ProcessOutcome(70, bytes(output["stdout"]), bytes(output["stderr"]), "timeout")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            return ProcessOutcome(70, bytes(output["stdout"]), bytes(output["stderr"]), "timeout")
        return ProcessOutcome(returncode, bytes(output["stdout"]), bytes(output["stderr"]))
    finally:
        selector.close()
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass
        if process.poll() is None:
            _kill_process_group(process)


def _canonical_request_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise LauncherError("invalid_request_id") from exc
    canonical = str(parsed)
    if value != canonical:
        raise LauncherError("invalid_request_id")
    return canonical


def metadata_is_safe(
    info,
    *,
    expected_uid: int,
    expected_mode: int,
    directory: bool,
    enforce_posix_mode: bool,
) -> bool:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        not stat.S_ISLNK(info.st_mode)
        and expected_type(info.st_mode)
        and info.st_uid == expected_uid
        and (not enforce_posix_mode or stat.S_IMODE(info.st_mode) == expected_mode)
    )


def inspect_request(
    config: LauncherConfig,
    request_id: str,
    *,
    expected_uid: int | None = None,
) -> PreparedRequest:
    request_id = _canonical_request_id(request_id)
    directory = config.runtime_root / request_id
    if directory.parent != config.runtime_root:
        raise LauncherError("invalid_request_path")
    try:
        directory_info = directory.lstat()
    except OSError as exc:
        raise LauncherError("request_unavailable") from exc
    owner_uid = config.request_creator_uid if expected_uid is None else expected_uid
    if not metadata_is_safe(
        directory_info,
        expected_uid=owner_uid,
        expected_mode=0o700,
        directory=True,
        enforce_posix_mode=os.name == "posix",
    ):
        raise LauncherError("unsafe_request_directory")
    try:
        names = {entry.name for entry in os.scandir(directory)}
    except OSError as exc:
        raise LauncherError("request_unavailable") from exc
    allowed_names = {"support-response.schema.json"} | IMAGE_NAMES
    if not names or not names <= allowed_names or "support-response.schema.json" not in names:
        raise LauncherError("unsafe_request_contents")
    images = names & IMAGE_NAMES
    if len(images) > 1:
        raise LauncherError("unsafe_request_contents")

    paths = [directory / "support-response.schema.json"]
    if images:
        paths.append(directory / next(iter(images)))
    for path in paths:
        try:
            info = path.lstat()
        except OSError as exc:
            raise LauncherError("request_unavailable") from exc
        if not metadata_is_safe(
            info,
            expected_uid=owner_uid,
            expected_mode=0o600,
            directory=False,
            enforce_posix_mode=os.name == "posix",
        ) or info.st_nlink != 1:
            raise LauncherError("unsafe_request_file")
    schema = paths[0]
    if schema.stat().st_size > 64 * 1024:
        raise LauncherError("unsafe_schema")
    try:
        schema_payload = json.loads(schema.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError("unsafe_schema") from exc
    if schema_payload != SCHEMA:
        raise LauncherError("unsafe_schema")
    image = paths[1] if len(paths) == 2 else None
    if image is not None and not 1 <= image.stat().st_size <= config.max_image_bytes:
        raise LauncherError("unsafe_image")
    return PreparedRequest(request_id, directory, schema, image, directory_info.st_ino)


def transfer_request_ownership(
    config: LauncherConfig, request: PreparedRequest, *, ai_uid: int, ai_gid: int
) -> PreparedRequest:
    if os.name != "posix":
        raise LauncherError("unsupported_platform")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(config.runtime_root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    try:
        directory_fd = os.open(
            request.request_id,
            os.O_RDONLY | os.O_DIRECTORY | nofollow,
            dir_fd=parent_fd,
        )
        try:
            current = os.fstat(directory_fd)
            if (
                current.st_ino != request.directory_inode
                or current.st_uid != config.request_creator_uid
                or stat.S_IMODE(current.st_mode) != 0o700
            ):
                raise LauncherError("request_changed")
            expected_names = {request.schema.name}
            if request.image is not None:
                expected_names.add(request.image.name)
            if set(os.listdir(directory_fd)) != expected_names:
                raise LauncherError("request_changed")
            for name in sorted(expected_names):
                file_fd = os.open(name, os.O_RDONLY | nofollow, dir_fd=directory_fd)
                try:
                    info = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != config.request_creator_uid
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1
                    ):
                        raise LauncherError("request_changed")
                    if name == request.schema.name:
                        if info.st_size > 64 * 1024:
                            raise LauncherError("request_changed")
                        schema_data = os.read(file_fd, 64 * 1024 + 1)
                        try:
                            schema_payload = json.loads(schema_data)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise LauncherError("request_changed") from exc
                        if schema_payload != SCHEMA:
                            raise LauncherError("request_changed")
                    elif not 1 <= info.st_size <= config.max_image_bytes:
                        raise LauncherError("request_changed")
                    os.fchown(file_fd, ai_uid, ai_gid)
                    os.fchmod(file_fd, 0o600)
                finally:
                    os.close(file_fd)
            os.fchown(directory_fd, ai_uid, ai_gid)
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)
    finally:
        os.close(parent_fd)
    return inspect_request(config, request.request_id, expected_uid=ai_uid)


class RequestLock:
    def __init__(self, config: LauncherConfig, request_id: str):
        self.path = config.lock_root / f"{request_id}.lock"
        self.fd: int | None = None

    def __enter__(self):
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        if fcntl is None:
            flags |= os.O_EXCL
        try:
            self.fd = os.open(self.path, flags, 0o600)
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or os.name == "posix"
                and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600)
            ):
                raise LauncherError("request_lock_failed")
            if fcntl is not None:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise LauncherError("request_active") from exc
            os.ftruncate(self.fd, 0)
            os.write(self.fd, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError as exc:
            raise LauncherError("request_active") from exc
        except LauncherError:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise
        except OSError as exc:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise LauncherError("request_lock_failed") from exc
        return self

    def __exit__(self, _type, _value, _traceback):
        if self.fd is not None:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        if fcntl is None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


def cleanup_request(request: PreparedRequest) -> None:
    if os.name != "posix" or shutil.rmtree.avoids_symlink_attacks is not True:
        return
    parent_fd = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(request.directory.parent, flags)
        info = os.stat(request.request_id, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and info.st_ino == request.directory_inode:
            shutil.rmtree(request.request_id, dir_fd=parent_fd)
    except OSError:
        pass
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def validate_runtime_permissions(config: LauncherConfig, *, ai_uid: int) -> None:
    checks = (
        (config.codex_binary, stat.S_ISREG, 0, None, "codex_binary_permissions"),
        (config.codex_home, stat.S_ISDIR, ai_uid, 0o700, "codex_home_permissions"),
        (config.runtime_root, stat.S_ISDIR, 0, REQUEST_ROOT_MODE, "runtime_root_permissions"),
        (config.lock_root, stat.S_ISDIR, 0, 0o700, "lock_root_permissions"),
    )
    for path, type_check, owner, expected_mode, error in checks:
        try:
            info = path.lstat()
        except OSError as exc:
            raise LauncherConfigurationError(error) from exc
        if stat.S_ISLNK(info.st_mode) or not type_check(info.st_mode) or info.st_uid != owner:
            raise LauncherConfigurationError(error)
        mode = stat.S_IMODE(info.st_mode)
        if expected_mode is not None and mode != expected_mode:
            raise LauncherConfigurationError(error)
        if path == config.codex_binary and mode & 0o022:
            raise LauncherConfigurationError(error)


class Launcher:
    def __init__(self, config: LauncherConfig, *, runner=run_bounded_process, health=check_health):
        self.config = config
        self.runner = runner
        self.health = health
        if pwd is None:
            raise LauncherConfigurationError("AI user database is unavailable")
        try:
            identity = pwd.getpwnam(config.ai_user)
        except KeyError as exc:
            raise LauncherConfigurationError("AI user is unavailable") from exc
        self.ai_uid = identity.pw_uid
        self.ai_gid = identity.pw_gid
        if self.ai_uid == config.request_creator_uid:
            raise LauncherConfigurationError("AI user must differ from the request creator")
        validate_runtime_permissions(config, ai_uid=self.ai_uid)

    def _limits(self) -> ProcessLimits:
        return ProcessLimits(
            self.config.timeout_seconds,
            self.config.max_stdout_bytes,
            self.config.max_stderr_bytes,
        )

    def _run(self, argv: list[str], *, input_data: bytes = b"", cwd: Path | None = None):
        return self.runner(
            argv,
            input_data=input_data,
            cwd=cwd or self.config.codex_home,
            environment=minimal_environment(self.config),
            limits=self._limits(),
            uid=self.ai_uid,
            gid=self.ai_gid,
        )

    def _health(self) -> HealthResult:
        return self.health(self.config, ai_uid=self.ai_uid)

    def _require_health(self) -> None:
        result = self._health()
        if result.status != "ok" or not result.direct_network_blocked:
            raise LauncherError(result.status)

    def _verified_version(self) -> ProcessOutcome:
        outcome = self._run(version_argv(self.config))
        if (
            outcome.returncode != 0
            or outcome.stdout != VERSION_OUTPUT
            or outcome.stderr
            or outcome.error
        ):
            raise LauncherError("version_mismatch")
        return outcome

    def _verified_login(self) -> ProcessOutcome:
        outcome = self._run(login_status_argv(self.config))
        if (
            outcome.returncode != 0
            or outcome.stdout
            or outcome.stderr not in CHATGPT_AUTH_OUTPUTS
            or outcome.error
        ):
            raise LauncherError("login_status_invalid")
        return outcome

    def capabilities(self) -> tuple[int, bytes]:
        health = self._health()
        if health.status == "ok" and health.direct_network_blocked:
            try:
                self._verified_version()
            except LauncherError:
                health = HealthResult("configuration_error", True)
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "launcher_version": LAUNCHER_VERSION,
            "codex_cli_version": CODEX_CLI_VERSION,
            "network_mode": "maxinik-proxy-only",
            "direct_network_blocked": health.direct_network_blocked,
            "proxy_health": health.status,
        }
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
        return (0 if health.status == "ok" else 70), encoded

    def execute(
        self, operation: str, *, request_id: str = "", prompt: bytes = b""
    ) -> ProcessOutcome:
        if operation not in {"version", "login-status", "exec-support-request"}:
            raise LauncherError("unsupported_operation", 64)
        self._require_health()
        version = self._verified_version()
        if operation == "version":
            return version
        login = self._verified_login()
        if operation == "login-status":
            return login
        if not prompt or len(prompt) > self.config.max_prompt_bytes:
            raise LauncherError("prompt_limit", 64)
        request_id = _canonical_request_id(request_id)
        prepared: PreparedRequest | None = None
        with RequestLock(self.config, request_id):
            inspected = inspect_request(self.config, request_id)
            try:
                prepared = inspected
                prepared = transfer_request_ownership(
                    self.config, inspected, ai_uid=self.ai_uid, ai_gid=self.ai_gid
                )
                return self._run(
                    exec_argv(self.config, prepared),
                    input_data=prompt,
                    cwd=prepared.directory,
                )
            finally:
                if prepared is not None:
                    cleanup_request(prepared)


def _safe_error(error: LauncherError) -> ProcessOutcome:
    return ProcessOutcome(error.returncode, b"", f"{error.code}\n".encode("ascii"), error.code)


def _dispatch(launcher: Launcher, operation: str, *, request_id: str = "", prompt: bytes = b""):
    try:
        return launcher.execute(operation, request_id=request_id, prompt=prompt)
    except LauncherError as exc:
        return _safe_error(exc)


def serve_one(launcher: Launcher, input_stream, output_stream) -> int:
    try:
        payload = validate_request(
            decode_frame(input_stream), max_prompt_bytes=launcher.config.max_prompt_bytes
        )
        operation = str(payload["operation"])
        if operation == "capabilities":
            returncode, stdout = launcher.capabilities()
            error = "" if returncode == 0 else "health_failed"
            outcome = ProcessOutcome(returncode, stdout, b"", error)
        else:
            outcome = _dispatch(
                launcher,
                operation,
                request_id=str(payload.get("request_id", "")),
                prompt=payload.get("prompt", b""),
            )
    except ProtocolError as exc:
        outcome = ProcessOutcome(64, b"", f"{exc}\n".encode("ascii"), str(exc))
    output_stream.write(
        encode_frame(
            response_payload(outcome.returncode, outcome.stdout, outcome.stderr, outcome.error)
        )
    )
    output_stream.flush()
    return 0


def _read_bounded(stream, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise LauncherError("prompt_limit", 64)
    return data


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    allowed = (
        arguments in (["version"], ["login-status"], ["capabilities", "--json"], ["socket-serve"])
        or len(arguments) == 2
        and arguments[0] == "exec-support-request"
    )
    if not allowed:
        print("unsupported_operation", file=sys.stderr)
        return 64
    if os.name != "posix" or os.geteuid() != 0:
        print("configuration_error", file=sys.stderr)
        return 70
    try:
        launcher = Launcher(load_launcher_config(LAUNCHER_CONFIG_PATH))
        if arguments == ["socket-serve"]:
            return serve_one(launcher, sys.stdin.buffer, sys.stdout.buffer)
        if arguments == ["capabilities", "--json"]:
            returncode, output = launcher.capabilities()
            sys.stdout.buffer.write(output)
            return returncode
        prompt = b""
        request_id = ""
        if arguments[0] == "exec-support-request":
            request_id = arguments[1]
            prompt = _read_bounded(sys.stdin.buffer, launcher.config.max_prompt_bytes)
        outcome = _dispatch(launcher, arguments[0], request_id=request_id, prompt=prompt)
        sys.stdout.buffer.write(outcome.stdout)
        sys.stderr.buffer.write(outcome.stderr)
        return outcome.returncode
    except (LauncherConfigurationError, LauncherError):
        print("configuration_error", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
