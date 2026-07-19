import base64
import json
import struct
from pathlib import Path

import pytest

from apps.ai_support.prompts import SYSTEM_RULES
from apps.ai_support.providers.base import SupportImage, SupportRequest
from apps.ai_support.providers.codex_cli import ProcessOutcome
from apps.ai_support.providers.external_launcher import (
    EXPECTED_HANDSHAKE,
    PROTOCOL_VERSION,
    ExternalCodexProvider,
    ExternalLauncherError,
    _decode_response,
    _encode_request,
    query_launcher_ready,
)

THREAD_ID = "0199a213-81c0-7800-8aa1-bbab2a035a53"


def support_request(**overrides):
    values = {
        "user_text": "Что означает ошибка?",
        "system_instruction": SYSTEM_RULES,
        "knowledge_chunks": ("Справочный текст",),
        "route_context": {"path": "/search/", "route_name": "part_search"},
        "user_role": "Кладовщик",
        "public_base_url": "https://185-250-44-206.sslip.io/",
    }
    values.update(overrides)
    return SupportRequest(**values)


def jsonl(answer="Безопасный ответ"):
    events = [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "agent_message",
                "text": json.dumps({"answer": answer}, ensure_ascii=False),
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 20,
                "reasoning_output_tokens": 0,
            },
        },
    ]
    return b"\n".join(json.dumps(event).encode() for event in events) + b"\n"


def make_provider(tmp_path, transport):
    workspace = tmp_path / "requests"
    workspace.mkdir()
    provider = ExternalCodexProvider(
        socket_path="/run/denstock-ai/launcher.sock",
        required_version="0.142.5",
        model="configured-model",
        workspace=workspace,
        timeout_seconds=60,
        max_output_bytes=65536,
        max_stderr_bytes=16384,
        max_prompt_chars=24000,
        max_history_chars=12000,
        global_concurrency=1,
        _transport=transport,
    )
    return provider, workspace


def ready_outcome():
    return ProcessOutcome(
        0,
        (json.dumps(EXPECTED_HANDSHAKE, sort_keys=True) + "\n").encode(),
        b"",
    )


def test_external_provider_uses_fixed_protocol_request_and_cleans_up(
    monkeypatch, tmp_path
):
    calls = []
    deadlines = []

    def transport(socket_path, payload, **kwargs):
        calls.append((socket_path, payload))
        deadlines.append(kwargs["deadline"])
        operation = payload["operation"]
        if operation == "capabilities":
            return ready_outcome()
        if operation == "login-status":
            return ProcessOutcome(0, b"", b"Logged in using ChatGPT\n")
        request_id = payload["request_id"]
        request_dir = workspace / request_id
        assert request_dir.name == request_id
        assert set(path.name for path in request_dir.iterdir()) == {
            "support-response.schema.json",
            "attachment.png",
        }
        assert json.loads(
            (request_dir / "support-response.schema.json").read_text(encoding="utf-8")
        ) == {
            "type": "object",
            "properties": {"answer": {"type": "string", "maxLength": 16000}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        assert (request_dir / "attachment.png").read_bytes() == b"normalized-image"
        assert "Что означает ошибка?" in base64.b64decode(payload["prompt_b64"]).decode()
        assert set(payload) == {
            "protocol_version",
            "operation",
            "request_id",
            "prompt_b64",
        }
        return ProcessOutcome(0, jsonl(), b"")

    provider, workspace = make_provider(tmp_path, transport)
    monkeypatch.setattr(
        "apps.ai_support.providers.external_launcher.validate_launcher_socket",
        lambda path: path,
    )

    result = provider.generate(
        support_request(image=SupportImage(b"normalized-image", "image/png"))
    )

    assert result.status == "completed"
    assert result.text == "Безопасный ответ"
    assert result.request_id == THREAD_ID
    assert [payload["operation"] for _, payload in calls] == [
        "capabilities",
        "login-status",
        "exec-support-request",
    ]
    assert len(set(deadlines)) == 1
    assert list(workspace.iterdir()) == []


def test_external_provider_fails_closed_before_request_creation(monkeypatch, tmp_path):
    def transport(*_args, **_kwargs):
        raise ExternalLauncherError("provider_unavailable")

    provider, workspace = make_provider(tmp_path, transport)
    monkeypatch.setattr(
        "apps.ai_support.providers.external_launcher.validate_launcher_socket",
        lambda path: path,
    )

    result = provider.generate(support_request())

    assert result.error_code == "provider_unavailable"
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    ("launcher_error", "expected"),
    [
        ("timeout", "provider_timeout"),
        ("stdout_limit", "provider_output_too_large"),
        ("stderr_limit", "provider_output_too_large"),
        ("prompt_limit", "provider_input_too_large"),
        ("unsafe_request_directory", "provider_unavailable"),
    ],
)
def test_external_provider_normalizes_launcher_failures(
    monkeypatch, tmp_path, launcher_error, expected
):
    def transport(_socket_path, payload, **_kwargs):
        if payload["operation"] == "capabilities":
            return ready_outcome()
        if payload["operation"] == "login-status":
            return ProcessOutcome(0, b"", b"Logged in using ChatGPT\n")
        return ProcessOutcome(70, b"", b"", launcher_error)

    provider, _workspace = make_provider(tmp_path, transport)
    monkeypatch.setattr(
        "apps.ai_support.providers.external_launcher.validate_launcher_socket",
        lambda path: path,
    )

    assert provider.generate(support_request()).error_code == expected


def test_protocol_frame_is_bounded_and_has_no_arbitrary_launcher_controls():
    frame = _encode_request(
        {"protocol_version": PROTOCOL_VERSION, "operation": "login-status"}
    )
    size = struct.unpack("!I", frame[:4])[0]

    assert size == len(frame[4:])
    assert json.loads(frame[4:]) == {
        "protocol_version": 1,
        "operation": "login-status",
    }
    with pytest.raises(ExternalLauncherError, match="provider_input_too_large"):
        _encode_request({"payload": "x" * (128 * 1024)})


def test_response_decoder_rejects_wrong_protocol_and_output_overflow():
    payload = {
        "protocol_version": 2,
        "returncode": 0,
        "stdout_b64": "",
        "stderr_b64": "",
        "error": "",
    }
    with pytest.raises(ExternalLauncherError, match="provider_invalid_output"):
        _decode_response(json.dumps(payload).encode(), max_stdout_bytes=10, max_stderr_bytes=10)

    payload["protocol_version"] = 1
    payload["stdout_b64"] = base64.b64encode(b"x" * 11).decode()
    with pytest.raises(ExternalLauncherError, match="provider_output_too_large"):
        _decode_response(json.dumps(payload).encode(), max_stdout_bytes=10, max_stderr_bytes=10)


@pytest.mark.parametrize(
    "outcome",
    [
        ProcessOutcome(0, b"{}", b""),
        ProcessOutcome(70, json.dumps(EXPECTED_HANDSHAKE).encode(), b""),
        ProcessOutcome(0, json.dumps(EXPECTED_HANDSHAKE).encode(), b"detail"),
    ],
)
def test_launcher_readiness_requires_exact_handshake(outcome):
    def transport(_path, payload, **_kwargs):
        if payload["operation"] == "capabilities":
            return outcome
        pytest.fail("login must not run after an invalid handshake")

    with pytest.raises(ExternalLauncherError, match="codex_cli_incompatible"):
        query_launcher_ready(
            Path("/run/denstock-ai/launcher.sock"),
            deadline=10,
            clock=lambda: 0,
            transport=transport,
        )


def test_launcher_readiness_rejects_api_key_auth_status():
    def transport(_path, payload, **_kwargs):
        if payload["operation"] == "capabilities":
            return ready_outcome()
        return ProcessOutcome(0, b"", b"Logged in using an API key - ***\n")

    with pytest.raises(ExternalLauncherError, match="codex_auth_status_unknown"):
        query_launcher_ready(
            Path("/run/denstock-ai/launcher.sock"),
            deadline=10,
            clock=lambda: 0,
            transport=transport,
        )
