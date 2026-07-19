import base64
import binascii
import json
import struct

from .constants import PROTOCOL_VERSION

MAX_FRAME_BYTES = 128 * 1024
ALLOWED_OPERATIONS = {"version", "login-status", "exec-support-request", "capabilities"}


class ProtocolError(ValueError):
    pass


def encode_frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large")
    return struct.pack("!I", len(encoded)) + encoded


def decode_frame(stream) -> dict[str, object]:
    header = stream.read(4)
    if len(header) != 4:
        raise ProtocolError("invalid_frame")
    size = struct.unpack("!I", header)[0]
    if not 1 <= size <= MAX_FRAME_BYTES:
        raise ProtocolError("invalid_frame")
    body = stream.read(size)
    if len(body) != size:
        raise ProtocolError("invalid_frame")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request")
    return payload


def validate_request(payload: dict[str, object], *, max_prompt_bytes: int) -> dict[str, object]:
    operation = payload.get("operation")
    if operation not in ALLOWED_OPERATIONS or payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_operation")
    allowed = {"protocol_version", "operation"}
    if operation == "capabilities":
        allowed.add("json")
        if payload.get("json") is not True:
            raise ProtocolError("invalid_request")
    elif operation == "exec-support-request":
        allowed.update({"request_id", "prompt_b64"})
        if not isinstance(payload.get("request_id"), str):
            raise ProtocolError("invalid_request")
        encoded_prompt = payload.get("prompt_b64")
        if not isinstance(encoded_prompt, str):
            raise ProtocolError("invalid_request")
        try:
            prompt = base64.b64decode(encoded_prompt, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProtocolError("invalid_request") from exc
        if not prompt or len(prompt) > max_prompt_bytes:
            raise ProtocolError("invalid_request")
        payload = {**payload, "prompt": prompt}
    if set(payload) - {"prompt"} != allowed:
        raise ProtocolError("invalid_request")
    return payload


def response_payload(returncode: int, stdout: bytes, stderr: bytes, error: str = "") -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "returncode": returncode,
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "error": error,
    }
