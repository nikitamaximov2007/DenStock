import base64
import binascii
import errno
import json
import os
import selectors
import shutil
import socket
import stat
import struct
import time
import uuid
from pathlib import Path

from apps.ai_support.contracts import AUDITED_CODEX_CLI_VERSION
from apps.ai_support.runtime import validated_local_directory

from .base import SupportRequest, SupportResult
from .codex_cli import (
    CHATGPT_AUTH_OUTPUTS,
    QUOTA_MARKERS,
    SCHEMA,
    SETTING_VERSION_PATTERN,
    CodexOutputError,
    ProcessOutcome,
    _build_prompt,
    _parse_result,
    _secure_write,
    _semaphore,
)

PROTOCOL_VERSION = 1
LAUNCHER_VERSION = "1.0.0"
MAX_FRAME_BYTES = 128 * 1024
MAX_LAUNCHER_PROMPT_BYTES = 64 * 1024
EXPECTED_HANDSHAKE = {
    "protocol_version": PROTOCOL_VERSION,
    "launcher_version": LAUNCHER_VERSION,
    "codex_cli_version": AUDITED_CODEX_CLI_VERSION,
    "network_mode": "maxinik-proxy-only",
    "direct_network_blocked": True,
    "proxy_health": "ok",
}
RESPONSE_KEYS = {
    "protocol_version",
    "returncode",
    "stdout_b64",
    "stderr_b64",
    "error",
}
LAUNCHER_ERROR_CODES = {
    "timeout": "provider_timeout",
    "stdout_limit": "provider_output_too_large",
    "stderr_limit": "provider_output_too_large",
    "prompt_limit": "provider_input_too_large",
    "request_active": "provider_capacity",
}


class ExternalLauncherError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _remaining(deadline: float, clock) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise ExternalLauncherError("provider_timeout")
    return remaining


def _wait_for(sock: socket.socket, event: int, *, deadline: float, clock) -> None:
    with selectors.DefaultSelector() as selector:
        selector.register(sock, event)
        if not selector.select(_remaining(deadline, clock)):
            raise ExternalLauncherError("provider_timeout")


def _send_all(sock: socket.socket, data: bytes, *, deadline: float, clock) -> None:
    sent = 0
    while sent < len(data):
        _wait_for(sock, selectors.EVENT_WRITE, deadline=deadline, clock=clock)
        try:
            count = sock.send(data[sent:])
        except BlockingIOError:
            continue
        if count <= 0:
            raise ExternalLauncherError("provider_unavailable")
        sent += count


def _receive_exact(
    sock: socket.socket, size: int, *, deadline: float, clock
) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        _wait_for(sock, selectors.EVENT_READ, deadline=deadline, clock=clock)
        try:
            chunk = sock.recv(size - len(chunks))
        except BlockingIOError:
            continue
        if not chunk:
            raise ExternalLauncherError("provider_unavailable")
        chunks.extend(chunk)
    return bytes(chunks)


def _encode_request(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(encoded) <= MAX_FRAME_BYTES:
        raise ExternalLauncherError("provider_input_too_large")
    return struct.pack("!I", len(encoded)) + encoded


def _decode_response(body: bytes, *, max_stdout_bytes: int, max_stderr_bytes: int):
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalLauncherError("provider_invalid_output") from exc
    if not isinstance(payload, dict) or set(payload) != RESPONSE_KEYS:
        raise ExternalLauncherError("provider_invalid_output")
    returncode = payload.get("returncode")
    error = payload.get("error")
    if (
        payload.get("protocol_version") != PROTOCOL_VERSION
        or type(returncode) is not int
        or not isinstance(error, str)
        or len(error) > 80
        or not error.isascii()
    ):
        raise ExternalLauncherError("provider_invalid_output")
    try:
        stdout = base64.b64decode(payload.get("stdout_b64"), validate=True)
        stderr = base64.b64decode(payload.get("stderr_b64"), validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ExternalLauncherError("provider_invalid_output") from exc
    if len(stdout) > max_stdout_bytes or len(stderr) > max_stderr_bytes:
        raise ExternalLauncherError("provider_output_too_large")
    return ProcessOutcome(returncode, stdout, stderr, error)


def exchange(
    socket_path: Path,
    payload: dict[str, object],
    *,
    deadline: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    clock=time.monotonic,
) -> ProcessOutcome:
    request = _encode_request(payload)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.setblocking(False)
            result = connection.connect_ex(str(socket_path))
            pending_errors = {
                0,
                errno.EAGAIN,
                errno.EINPROGRESS,
                errno.EWOULDBLOCK,
            }
            if result not in pending_errors:
                raise ExternalLauncherError("provider_unavailable")
            if result:
                _wait_for(
                    connection,
                    selectors.EVENT_WRITE,
                    deadline=deadline,
                    clock=clock,
                )
                if connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    raise ExternalLauncherError("provider_unavailable")
            _send_all(connection, request, deadline=deadline, clock=clock)
            header = _receive_exact(connection, 4, deadline=deadline, clock=clock)
            size = struct.unpack("!I", header)[0]
            if not 1 <= size <= MAX_FRAME_BYTES:
                raise ExternalLauncherError("provider_invalid_output")
            body = _receive_exact(connection, size, deadline=deadline, clock=clock)
    except ExternalLauncherError:
        raise
    except OSError as exc:
        raise ExternalLauncherError("provider_unavailable") from exc
    return _decode_response(
        body,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
    )


def _operation(operation: str, **values) -> dict[str, object]:
    return {"protocol_version": PROTOCOL_VERSION, "operation": operation, **values}


def _decode_handshake(outcome: ProcessOutcome) -> dict[str, object]:
    try:
        payload = json.loads(outcome.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalLauncherError("codex_cli_incompatible") from exc
    if (
        outcome.returncode != 0
        or outcome.stderr
        or outcome.error_code
        or payload != EXPECTED_HANDSHAKE
    ):
        raise ExternalLauncherError("codex_cli_incompatible")
    return payload


def query_launcher_ready(
    socket_path: Path,
    *,
    deadline: float,
    clock=time.monotonic,
    transport=exchange,
) -> dict[str, object]:
    capabilities = transport(
        socket_path,
        _operation("capabilities", json=True),
        deadline=deadline,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        clock=clock,
    )
    handshake = _decode_handshake(capabilities)
    login = transport(
        socket_path,
        _operation("login-status"),
        deadline=deadline,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        clock=clock,
    )
    if (
        login.returncode != 0
        or login.stdout
        or login.stderr not in CHATGPT_AUTH_OUTPUTS
        or login.error_code
    ):
        raise ExternalLauncherError("codex_auth_status_unknown")
    return handshake


def validate_launcher_socket(path: Path) -> Path:
    if os.name != "posix" or not path.is_absolute():
        raise ExternalLauncherError("provider_not_configured")
    try:
        info = path.lstat()
        parent = path.parent.lstat()
    except OSError as exc:
        raise ExternalLauncherError("provider_not_configured") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o660
        or stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) & 0o002
    ):
        raise ExternalLauncherError("provider_not_configured")
    return path


def _safe_cleanup(workspace: Path, request_id: str) -> None:
    candidate = workspace / request_id
    if not candidate.exists():
        return
    if os.name == "posix" and shutil.rmtree.avoids_symlink_attacks is True:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(workspace, flags)
        try:
            info = os.stat(request_id, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(request_id, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        return
    if candidate.is_dir() and not candidate.is_symlink():
        shutil.rmtree(candidate)


class ExternalCodexProvider:
    def __init__(
        self,
        *,
        socket_path,
        required_version: str,
        model: str,
        workspace,
        timeout_seconds: int,
        max_output_bytes: int,
        max_stderr_bytes: int,
        max_prompt_chars: int,
        max_history_chars: int,
        global_concurrency: int,
        _clock=None,
        _transport=exchange,
    ):
        self.socket_path = Path(socket_path)
        self.required_version = required_version
        self.model = model
        self.workspace = Path(workspace)
        self.timeout_seconds = max(timeout_seconds, 1)
        self.max_output_bytes = max(max_output_bytes, 1024)
        self.max_stderr_bytes = max(max_stderr_bytes, 1024)
        self.max_prompt_chars = max(max_prompt_chars, 1000)
        self.max_history_chars = max(max_history_chars, 0)
        self.slots = _semaphore(max(global_concurrency, 1))
        self._clock = _clock or time.monotonic
        self._transport = _transport

    def _error(self, code: str, started: float) -> SupportResult:
        return SupportResult(
            text="ИИ-поддержка временно недоступна. Создайте обращение разработчику.",
            provider="codex_cli",
            model=self.model,
            status="failed",
            latency_ms=int((self._clock() - started) * 1000),
            error_code=code,
        )

    def _call(self, payload, *, deadline, stdout_limit, stderr_limit):
        return self._transport(
            self.socket_path,
            payload,
            deadline=deadline,
            max_stdout_bytes=stdout_limit,
            max_stderr_bytes=stderr_limit,
            clock=self._clock,
        )

    def generate(self, request: SupportRequest) -> SupportResult:
        started = self._clock()
        deadline = started + self.timeout_seconds
        if (
            not self.model
            or not SETTING_VERSION_PATTERN.fullmatch(self.required_version)
            or self.required_version != AUDITED_CODEX_CLI_VERSION
        ):
            return self._error("codex_cli_incompatible", started)
        if not self.slots.acquire(blocking=False):
            return self._error("provider_capacity", started)
        request_id = ""
        workspace = None
        try:
            try:
                workspace = validated_local_directory(self.workspace)
                validate_launcher_socket(self.socket_path)
                prompt = _build_prompt(
                    request, self.max_prompt_chars, self.max_history_chars
                ).encode("utf-8")
                if len(prompt) > MAX_LAUNCHER_PROMPT_BYTES:
                    return self._error("provider_input_too_large", started)
                query_launcher_ready(
                    self.socket_path,
                    deadline=deadline,
                    clock=self._clock,
                    transport=self._transport,
                )
            except ExternalLauncherError as exc:
                return self._error(exc.code, started)
            except (OSError, ValueError):
                return self._error("provider_not_configured", started)

            request_id = str(uuid.uuid4())
            request_dir = workspace / request_id
            try:
                request_dir.mkdir(mode=0o700)
                os.chmod(request_dir, 0o700)
                _secure_write(
                    request_dir / "support-response.schema.json",
                    json.dumps(SCHEMA, ensure_ascii=False).encode("utf-8"),
                )
                if request.image:
                    suffix = {
                        "image/jpeg": ".jpg",
                        "image/png": ".png",
                        "image/webp": ".webp",
                    }.get(request.image.mime_type)
                    if not suffix:
                        return self._error("rejected_image", started)
                    _secure_write(request_dir / f"attachment{suffix}", request.image.content)
                outcome = self._call(
                    _operation(
                        "exec-support-request",
                        request_id=request_id,
                        prompt_b64=base64.b64encode(prompt).decode("ascii"),
                    ),
                    deadline=deadline,
                    stdout_limit=self.max_output_bytes,
                    stderr_limit=self.max_stderr_bytes,
                )
            except ExternalLauncherError as exc:
                return self._error(exc.code, started)
            except OSError:
                return self._error("provider_unavailable", started)
            if outcome.error_code:
                return self._error(
                    LAUNCHER_ERROR_CODES.get(
                        outcome.error_code,
                        "provider_unavailable",
                    ),
                    started,
                )
            combined_error = (outcome.stdout + outcome.stderr).decode(
                "utf-8", errors="replace"
            ).lower()
            if outcome.returncode != 0:
                code = (
                    "subscription_quota_exceeded"
                    if any(marker in combined_error for marker in QUOTA_MARKERS)
                    else "provider_unavailable"
                )
                return self._error(code, started)
            try:
                answer, usage, codex_request_id = _parse_result(
                    outcome.stdout,
                    deadline=deadline,
                    _clock=self._clock,
                )
            except CodexOutputError as exc:
                return self._error(exc.code, started)
            if self._clock() >= deadline:
                return self._error("provider_timeout", started)
            return SupportResult(
                text=answer,
                provider="codex_cli",
                model=self.model,
                status="completed",
                latency_ms=int((self._clock() - started) * 1000),
                usage=usage,
                request_id=codex_request_id,
            )
        finally:
            if workspace is not None and request_id:
                try:
                    _safe_cleanup(workspace, request_id)
                except OSError:
                    pass
            self.slots.release()
