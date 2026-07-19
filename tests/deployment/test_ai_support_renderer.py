import json
import os
import stat
from io import StringIO

import pytest
from denstock_ai_network.renderer import (
    MAX_SECRET_SOURCE_BYTES,
    ConfigurationError,
    atomic_write_json,
    build_sing_box_config,
    main,
    parse_env,
    read_secret_source,
    redacted_summary,
    validate_values,
)


@pytest.fixture
def valid_raw_values():
    return {
        "MAXINIK_SERVER": "vpn.invalid",
        "MAXINIK_PORT": "443",
        "MAXINIK_UUID": "00000000-0000-4000-8000-000000000001",
        "MAXINIK_FLOW": "xtls-rprx-vision",
        "MAXINIK_REALITY_PUBLIC_KEY": "A" * 43,
        "MAXINIK_REALITY_SHORT_ID": "0123456789abcdef",
        "MAXINIK_REALITY_SNI": "cover.invalid",
        "MAXINIK_FINGERPRINT": "chrome",
        "MAXINIK_LOCAL_PROXY_HOST": "127.0.0.1",
        "MAXINIK_LOCAL_PROXY_PORT": "2080",
        "MAXINIK_ALPN": "h2,http/1.1",
        "MAXINIK_PACKET_ENCODING": "xudp",
        "MAXINIK_TRANSPORT_TYPE": "tcp",
        "MAXINIK_TRANSPORT_PATH": "",
    }


def test_renderer_accepts_documented_placeholders(valid_raw_values):
    config = build_sing_box_config(validate_values(valid_raw_values))

    assert config["inbounds"] == [
        {
            "type": "mixed",
            "tag": "local-mixed",
            "listen": "127.0.0.1",
            "listen_port": 2080,
            "set_system_proxy": False,
        }
    ]
    outbound = config["outbounds"][0]
    assert outbound["type"] == "vless"
    assert outbound["flow"] == "xtls-rprx-vision"
    assert outbound["tls"]["reality"]["enabled"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MAXINIK_UUID", "not-a-uuid"),
        ("MAXINIK_PORT", "0"),
        ("MAXINIK_PORT", "65536"),
        ("MAXINIK_LOCAL_PROXY_PORT", "80"),
        ("MAXINIK_REALITY_PUBLIC_KEY", "not-a-key"),
        ("MAXINIK_REALITY_SHORT_ID", "xyz"),
        ("MAXINIK_REALITY_SHORT_ID", "123"),
        ("MAXINIK_LOCAL_PROXY_HOST", "0.0.0.0"),
        ("MAXINIK_FLOW", ""),
        ("MAXINIK_TRANSPORT_TYPE", "ws"),
    ],
)
def test_renderer_rejects_invalid_values(valid_raw_values, field, value):
    valid_raw_values[field] = value
    with pytest.raises(ConfigurationError):
        validate_values(valid_raw_values)


def test_renderer_rejects_missing_secret(valid_raw_values):
    valid_raw_values.pop("MAXINIK_UUID")
    with pytest.raises(ConfigurationError, match="MAXINIK_UUID"):
        validate_values(valid_raw_values)


def test_env_parser_rejects_unknown_and_duplicate_settings():
    with pytest.raises(ConfigurationError, match="unsupported"):
        parse_env("UNSAFE=value")
    with pytest.raises(ConfigurationError, match="duplicate"):
        parse_env("MAXINIK_PORT=443\nMAXINIK_PORT=8443")


def test_renderer_has_no_tun_or_system_proxy(valid_raw_values):
    encoded = json.dumps(build_sing_box_config(validate_values(valid_raw_values))).lower()
    assert '"type": "tun"' not in encoded
    assert '"set_system_proxy": true' not in encoded
    assert "default route" not in encoded


def test_atomic_write_replaces_file_and_sets_mode_0600(tmp_path, valid_raw_values):
    target = tmp_path / "sing-box.json"
    target.write_text("old", encoding="utf-8")
    old_inode = target.stat().st_ino

    atomic_write_json(target, build_sing_box_config(validate_values(valid_raw_values)))

    assert json.loads(target.read_text(encoding="utf-8"))["inbounds"][0]["listen_port"] == 2080
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_ino != old_inode
    assert not list(tmp_path.glob(".sing-box.json.*"))


def test_atomic_write_refuses_symlink(tmp_path, valid_raw_values):
    real = tmp_path / "real.json"
    real.write_text("untouched", encoding="utf-8")
    linked = tmp_path / "linked.json"
    try:
        linked.symlink_to(real)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ConfigurationError, match="symlink"):
        atomic_write_json(linked, build_sing_box_config(validate_values(valid_raw_values)))
    assert real.read_text(encoding="utf-8") == "untouched"


def test_secret_source_symlink_is_rejected(tmp_path):
    real = tmp_path / "maxinik.env"
    real.write_text("MAXINIK_PORT=443", encoding="utf-8")
    linked = tmp_path / "linked.env"
    try:
        linked.symlink_to(real)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ConfigurationError, match="non-symlink"):
        read_secret_source(linked, StringIO())


def test_secret_source_size_is_bounded():
    oversized = "#" + "x" * MAX_SECRET_SOURCE_BYTES

    with pytest.raises(ConfigurationError, match="too large"):
        read_secret_source(None, StringIO(oversized))


def test_redacted_output_contains_no_secrets(valid_raw_values):
    config = build_sing_box_config(validate_values(valid_raw_values))
    output = json.dumps(redacted_summary(config))

    for field in (
        "MAXINIK_UUID",
        "MAXINIK_REALITY_PUBLIC_KEY",
        "MAXINIK_REALITY_SHORT_ID",
        "MAXINIK_SERVER",
        "MAXINIK_REALITY_SNI",
    ):
        assert valid_raw_values[field] not in output


def test_check_mode_prints_only_redacted_status(tmp_path, capsys, valid_raw_values):
    source = tmp_path / "maxinik.env"
    source.write_text("\n".join(f"{key}={value}" for key, value in valid_raw_values.items()))
    os.chmod(source, 0o600)

    assert main(["--env-file", str(source), "--check"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "configuration valid; secrets redacted\n"
    assert captured.err == ""
    assert valid_raw_values["MAXINIK_UUID"] not in captured.out


def test_invalid_secret_never_appears_in_error(tmp_path, capsys, valid_raw_values):
    secret = "very-private-value"
    valid_raw_values["MAXINIK_REALITY_PUBLIC_KEY"] = secret
    source = tmp_path / "maxinik.env"
    source.write_text("\n".join(f"{key}={value}" for key, value in valid_raw_values.items()))
    os.chmod(source, 0o600)

    assert main(["--env-file", str(source), "--check"]) == 2

    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_apply_mode_rejects_arbitrary_output_path(tmp_path, capsys, valid_raw_values):
    source = tmp_path / "maxinik.env"
    source.write_text("\n".join(f"{key}={value}" for key, value in valid_raw_values.items()))
    os.chmod(source, 0o600)

    assert main(["--env-file", str(source), "--output", str(tmp_path / "other.json")]) == 2

    captured = capsys.readouterr()
    assert "final output path" in captured.err
    assert valid_raw_values["MAXINIK_UUID"] not in captured.out + captured.err


def test_example_file_is_valid_and_contains_only_placeholders(project_root):
    example = project_root / "deploy" / "ai-support" / "maxinik.env.example"
    values = parse_env(example.read_text(encoding="utf-8"))

    validate_values(values)
    assert values["MAXINIK_SERVER"].endswith(".invalid")
    assert values["MAXINIK_REALITY_SNI"].endswith(".invalid")
