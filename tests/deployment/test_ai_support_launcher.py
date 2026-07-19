import base64
import io
import json
import os
import stat
import uuid
from types import SimpleNamespace

import pytest
from denstock_ai_network.config import LauncherConfig
from denstock_ai_network.constants import CODEX_CONFIG_OVERRIDES
from denstock_ai_network.health import HealthResult
from denstock_ai_network.launcher import (
    IMAGE_NAMES,
    SCHEMA,
    Launcher,
    LauncherError,
    PreparedRequest,
    ProcessOutcome,
    RequestLock,
    config_args,
    exec_argv,
    inspect_request,
    login_status_argv,
    main,
    metadata_is_safe,
    minimal_environment,
    serve_one,
    version_argv,
)
from denstock_ai_network.protocol import (
    ProtocolError,
    decode_frame,
    encode_frame,
    validate_request,
)

from apps.ai_support.providers.codex_cli import (
    CODEX_CONFIG_OVERRIDES as AUDITED_CODEX_CONFIG_OVERRIDES,
)


@pytest.fixture
def launcher_config(tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("placeholder", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    runtime_root = tmp_path / "requests"
    lock_root = tmp_path / "locks"
    for path in (codex_home, runtime_root, lock_root):
        path.mkdir()
    owner = runtime_root.stat().st_uid
    return LauncherConfig(
        protocol_version=1,
        codex_binary=codex,
        codex_cli_version="0.142.5",
        model="fixed-model",
        codex_home=codex_home,
        runtime_root=runtime_root,
        lock_root=lock_root,
        ai_user="denstock-ai",
        request_creator_uid=owner,
        proxy_host="127.0.0.1",
        proxy_port=2080,
        timeout_seconds=60,
        max_prompt_bytes=24000,
        max_stdout_bytes=65536,
        max_stderr_bytes=16384,
        max_image_bytes=1024,
        proxy_service="denstock-ai-proxy.service",
        firewall_table="denstock_ai",
    )


def make_request(config, *, image_name=None, image_content=b"image"):
    request_id = str(uuid.uuid4())
    directory = config.runtime_root / request_id
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    schema = directory / "support-response.schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    os.chmod(schema, 0o600)
    if image_name:
        image = directory / image_name
        image.write_bytes(image_content)
        os.chmod(image, 0o600)
    return request_id, directory


def test_environment_is_a_fixed_allowlist(launcher_config, monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")

    environment = minimal_environment(launcher_config)

    assert set(environment) == {
        "CODEX_HOME",
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:2080"
    assert environment["ALL_PROXY"] == "http://127.0.0.1:2080"
    assert "must-not-leak" not in environment.values()


def test_launcher_commands_use_fixed_binary_model_and_overrides(launcher_config):
    request = PreparedRequest(
        str(uuid.uuid4()),
        launcher_config.runtime_root / "request",
        launcher_config.runtime_root / "request" / "support-response.schema.json",
        None,
        1,
    )

    assert version_argv(launcher_config) == [str(launcher_config.codex_binary), "--version"]
    assert login_status_argv(launcher_config)[-2:] == ["login", "status"]
    argv = exec_argv(launcher_config, request)
    assert argv[-1] == "-"
    assert argv[argv.index("--model") + 1] == "fixed-model"
    assert argv[argv.index("--cd") + 1] == str(request.directory)
    assert 'approval_policy="never"' in argv
    assert "--sandbox" in argv and "read-only" in argv
    assert all("user-prompt" not in argument for argument in argv)
    assert config_args().count("-c") > 20


def test_launcher_overrides_match_the_audited_provider_contract():
    assert CODEX_CONFIG_OVERRIDES == AUDITED_CODEX_CONFIG_OVERRIDES


def test_launcher_image_must_come_from_prepared_request(launcher_config):
    image = launcher_config.runtime_root / "request" / "attachment.png"
    request = PreparedRequest(
        str(uuid.uuid4()), image.parent, image.parent / "support-response.schema.json", image, 1
    )

    argv = exec_argv(launcher_config, request)

    assert argv[argv.index("--image") + 1] == str(image)
    assert set(IMAGE_NAMES) == {
        "attachment.png",
        "attachment.jpg",
        "attachment.jpeg",
        "attachment.webp",
    }


def test_request_path_is_derived_from_canonical_uuid(launcher_config):
    request_id, directory = make_request(launcher_config)

    prepared = inspect_request(launcher_config, request_id)

    assert prepared.directory == directory
    assert prepared.schema.parent == directory


@pytest.mark.parametrize("request_id", ["../outside", "not-a-uuid", str(uuid.uuid4()).upper()])
def test_request_path_traversal_and_noncanonical_ids_are_rejected(launcher_config, request_id):
    with pytest.raises(LauncherError, match="invalid_request_id"):
        inspect_request(launcher_config, request_id)


def test_request_directory_symlink_is_rejected(launcher_config, tmp_path):
    request_id = str(uuid.uuid4())
    real = tmp_path / "outside"
    real.mkdir()
    try:
        (launcher_config.runtime_root / request_id).symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(LauncherError, match="unsafe_request_directory"):
        inspect_request(launcher_config, request_id)


def test_request_file_symlink_is_rejected(launcher_config, tmp_path):
    request_id = str(uuid.uuid4())
    directory = launcher_config.runtime_root / request_id
    directory.mkdir()
    os.chmod(directory, 0o700)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(SCHEMA), encoding="utf-8")
    try:
        (directory / "support-response.schema.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(LauncherError, match="unsafe_request_file"):
        inspect_request(launcher_config, request_id)


def test_request_wrong_owner_or_mode_is_rejected(launcher_config):
    if os.name != "posix":
        pytest.skip("POSIX modes are not represented by Windows stat")
    request_id, directory = make_request(launcher_config)
    os.chmod(directory, 0o755)
    with pytest.raises(LauncherError, match="unsafe_request_directory"):
        inspect_request(launcher_config, request_id)

    safe_mode = stat.S_IMODE(directory.stat().st_mode)
    assert safe_mode != 0o700


def test_posix_metadata_validator_rejects_wrong_owner_and_mode():
    safe = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=2001)
    wrong_owner = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=2002)
    wrong_mode = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=2001)

    assert metadata_is_safe(
        safe,
        expected_uid=2001,
        expected_mode=0o700,
        directory=True,
        enforce_posix_mode=True,
    )
    assert not metadata_is_safe(
        wrong_owner,
        expected_uid=2001,
        expected_mode=0o700,
        directory=True,
        enforce_posix_mode=True,
    )
    assert not metadata_is_safe(
        wrong_mode,
        expected_uid=2001,
        expected_mode=0o700,
        directory=True,
        enforce_posix_mode=True,
    )


def test_request_unexpected_image_name_is_rejected(launcher_config):
    request_id, directory = make_request(launcher_config)
    image = directory / "arbitrary-path.png"
    image.write_bytes(b"image")
    os.chmod(image, 0o600)

    with pytest.raises(LauncherError, match="unsafe_request_contents"):
        inspect_request(launcher_config, request_id)


def test_request_schema_is_fixed_not_user_controlled(launcher_config):
    request_id, directory = make_request(launcher_config)
    (directory / "support-response.schema.json").write_text("{}", encoding="utf-8")

    with pytest.raises(LauncherError, match="unsafe_schema"):
        inspect_request(launcher_config, request_id)


def test_request_image_size_is_bounded(launcher_config):
    request_id, _directory = make_request(
        launcher_config, image_name="attachment.png", image_content=b"x" * 1025
    )

    with pytest.raises(LauncherError, match="unsafe_image"):
        inspect_request(launcher_config, request_id)


def test_request_lock_rejects_duplicate_active_request(launcher_config):
    request_id = str(uuid.uuid4())

    with RequestLock(launcher_config, request_id):
        with pytest.raises(LauncherError, match="request_active"):
            with RequestLock(launcher_config, request_id):
                pass
    with RequestLock(launcher_config, request_id):
        pass


def build_launcher(monkeypatch, launcher_config, *, health_status="ok", version_ok=True):
    monkeypatch.setattr(
        "denstock_ai_network.launcher.pwd",
        SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=1201, pw_gid=1201)),
    )
    monkeypatch.setattr(
        "denstock_ai_network.launcher.validate_runtime_permissions", lambda *_a, **_k: None
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--version":
            output = b"codex-cli 0.142.5\n" if version_ok else b"codex-cli 9.9.9\n"
            return ProcessOutcome(0, output, b"")
        return ProcessOutcome(0, b"", b"Logged in using ChatGPT\n")

    direct_blocked = health_status != "direct_network_not_blocked"
    def health(*_args, **_kwargs):
        return HealthResult(health_status, direct_blocked)

    return Launcher(launcher_config, runner=runner, health=health), calls


def test_handshake_has_fixed_schema_and_no_secrets(monkeypatch, launcher_config):
    launcher, _calls = build_launcher(monkeypatch, launcher_config)

    returncode, output = launcher.capabilities()
    payload = json.loads(output)

    assert returncode == 0
    assert payload == {
        "protocol_version": 1,
        "launcher_version": "1.0.0",
        "codex_cli_version": "0.142.5",
        "network_mode": "maxinik-proxy-only",
        "direct_network_blocked": True,
        "proxy_health": "ok",
    }
    encoded = output.lower()
    assert b"uuid" not in encoded
    assert b"public_key" not in encoded
    assert b"server" not in encoded
    assert b"path" not in encoded


def test_handshake_fails_closed_on_version_mismatch(monkeypatch, launcher_config):
    launcher, _calls = build_launcher(monkeypatch, launcher_config, version_ok=False)

    returncode, output = launcher.capabilities()

    assert returncode == 70
    assert json.loads(output)["proxy_health"] == "configuration_error"


@pytest.mark.parametrize("health_status", ["proxy_unavailable", "direct_network_not_blocked"])
def test_launcher_does_not_start_codex_when_network_is_unsafe(
    monkeypatch, launcher_config, health_status
):
    launcher, calls = build_launcher(monkeypatch, launcher_config, health_status=health_status)

    with pytest.raises(LauncherError, match=health_status):
        launcher.execute("version")

    assert calls == []


def test_unknown_subcommands_and_arbitrary_flags_are_rejected_before_execution():
    assert main(["shell"]) == 64
    assert main(["exec", "-c", 'approval_policy="on-request"']) == 64
    assert main(["version", "--cd", "/tmp"]) == 64
    assert main(["resume"]) == 64


def test_protocol_rejects_unknown_keys_arbitrary_cwd_and_config():
    base = {"protocol_version": 1, "operation": "version"}
    for key in ("cwd", "-c", "model", "image", "environment"):
        with pytest.raises(ProtocolError, match="invalid_request"):
            validate_request({**base, key: "unsafe"}, max_prompt_bytes=100)


def test_protocol_prompt_is_bounded_and_not_an_argument():
    payload = {
        "protocol_version": 1,
        "operation": "exec-support-request",
        "request_id": str(uuid.uuid4()),
        "prompt_b64": base64.b64encode(b"safe prompt").decode(),
    }

    validated = validate_request(payload, max_prompt_bytes=100)

    assert validated["prompt"] == b"safe prompt"
    with pytest.raises(ProtocolError):
        oversized = {**payload, "prompt_b64": base64.b64encode(b"x" * 101).decode()}
        validate_request(oversized, max_prompt_bytes=100)


def test_protocol_frame_round_trip():
    payload = {"protocol_version": 1, "operation": "capabilities", "json": True}

    assert decode_frame(io.BytesIO(encode_frame(payload))) == payload


def test_socket_capabilities_returns_framed_handshake(monkeypatch, launcher_config):
    launcher, _calls = build_launcher(monkeypatch, launcher_config)
    source = io.BytesIO(
        encode_frame({"protocol_version": 1, "operation": "capabilities", "json": True})
    )
    destination = io.BytesIO()

    assert serve_one(launcher, source, destination) == 0

    destination.seek(0)
    response = decode_frame(destination)
    assert response["returncode"] == 0
    handshake = json.loads(base64.b64decode(response["stdout_b64"]))
    assert handshake["network_mode"] == "maxinik-proxy-only"


def test_process_runner_never_uses_shell_true(project_root):
    source = (
        project_root
        / "scripts"
        / "ai-support"
        / "denstock_ai_network"
        / "launcher.py"
    ).read_text(encoding="utf-8")

    assert "shell=False" in source
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "subprocess.call" not in source


def test_ownership_transfer_revalidates_open_file_descriptors(project_root):
    source = (
        project_root
        / "scripts"
        / "ai-support"
        / "denstock_ai_network"
        / "launcher.py"
    ).read_text(encoding="utf-8")

    assert "set(os.listdir(directory_fd)) != expected_names" in source
    assert "os.open(name, os.O_RDONLY | nofollow, dir_fd=directory_fd)" in source
    assert "info = os.fstat(file_fd)" in source
    assert "schema_data = os.read(file_fd, 64 * 1024 + 1)" in source
    assert "shutil.rmtree.avoids_symlink_attacks is not True" in source
    assert "shutil.rmtree(request.request_id, dir_fd=parent_fd)" in source


def test_launcher_handshake_revalidates_its_root_owned_installation(project_root):
    source = (
        project_root
        / "scripts"
        / "ai-support"
        / "denstock_ai_network"
        / "launcher.py"
    ).read_text(encoding="utf-8")

    assert 'Path("/usr/local/sbin/denstock-ai-launcher")' in source
    assert 'raise LauncherConfigurationError("launcher_permissions")' in source
    assert "info.st_uid != 0" in source
    assert "stat.S_IMODE(info.st_mode) & 0o022" in source
