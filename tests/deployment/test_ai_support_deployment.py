import hashlib
import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import denstock_ai_network.installer as installer
import denstock_ai_network.verification as verification
import pytest
from denstock_ai_network.constants import (
    CODEX_ARCHIVE_MEMBER,
    CODEX_ARCHIVE_NAME,
    CODEX_ARCHIVE_SHA256,
    CODEX_ARCHIVE_URL,
    CODEX_BINARY_BYTES,
    CODEX_BINARY_SHA256,
    CODEX_CLI_VERSION,
    REQUEST_ROOT_MODE,
    SING_BOX_DEB_NAME,
    SING_BOX_DEB_SHA256,
    SING_BOX_DEB_URL,
    SING_BOX_VERSION,
)
from denstock_ai_network.installer import (
    InstallationError,
    dry_run_plan,
    extract_codex_binary,
    launcher_payload,
    verify_codex_archive,
    verify_sing_box_package,
)
from denstock_ai_network.rollback import RollbackError, apply_rollback, rollback_plan
from denstock_ai_network.verification import EXPECTED_HANDSHAKE, VerificationError


@pytest.fixture
def deploy_root(project_root):
    return project_root / "deploy" / "ai-support"


def test_sing_box_release_is_exactly_pinned():
    assert SING_BOX_VERSION == "1.13.14"
    assert SING_BOX_DEB_NAME == "sing-box_1.13.14_linux_amd64.deb"
    assert SING_BOX_DEB_URL == (
        "https://github.com/SagerNet/sing-box/releases/download/v1.13.14/"
        "sing-box_1.13.14_linux_amd64.deb"
    )
    assert SING_BOX_DEB_SHA256 == (
        "320523f9586877c4cb244df753d848356787e15f2f4e23a00908af2422206542"
    )


def test_sing_box_package_checksum_is_verified_before_install(tmp_path):
    package = tmp_path / SING_BOX_DEB_NAME
    package.write_bytes(b"not the official package")
    assert hashlib.sha256(package.read_bytes()).hexdigest() != SING_BOX_DEB_SHA256

    with pytest.raises(InstallationError, match="checksum"):
        verify_sing_box_package(package)


def test_codex_release_is_exactly_pinned():
    assert CODEX_CLI_VERSION == "0.142.5"
    assert CODEX_ARCHIVE_NAME == "codex-x86_64-unknown-linux-musl.tar.gz"
    assert CODEX_ARCHIVE_URL == (
        "https://github.com/openai/codex/releases/download/rust-v0.142.5/"
        "codex-x86_64-unknown-linux-musl.tar.gz"
    )
    assert CODEX_ARCHIVE_SHA256 == (
        "cb933ec3cb61bf4b5fc88eecf5e6149829faa6172535b6ef0afb0154beb4aab8"
    )
    assert CODEX_BINARY_BYTES == 285_929_520
    assert CODEX_BINARY_SHA256 == (
        "ac06f492f3ded7a8e2f36dc961e3cc5276a3c4841a2695d4681d0557c5b30e41"
    )


def test_codex_archive_checksum_is_verified_before_install(tmp_path):
    archive = tmp_path / CODEX_ARCHIVE_NAME
    archive.write_bytes(b"not the official archive")

    with pytest.raises(InstallationError, match="checksum"):
        verify_codex_archive(archive)


def _write_tar(archive, members):
    with tarfile.open(archive, "w:gz") as output:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(content)
            output.addfile(info, io.BytesIO(content) if kind == tarfile.REGTYPE else None)


def test_codex_extraction_accepts_only_the_fixed_regular_member(tmp_path):
    archive = tmp_path / "codex.tar.gz"
    destination = tmp_path / "codex"
    _write_tar(archive, [(CODEX_ARCHIVE_MEMBER, b"binary", tarfile.REGTYPE)])

    extract_codex_binary(archive, destination)

    assert destination.read_bytes() == b"binary"


@pytest.mark.parametrize(
    "members",
    [
        [("../codex", b"binary", tarfile.REGTYPE)],
        [(CODEX_ARCHIVE_MEMBER, b"", tarfile.SYMTYPE)],
        [
            (CODEX_ARCHIVE_MEMBER, b"binary", tarfile.REGTYPE),
            ("extra", b"extra", tarfile.REGTYPE),
        ],
    ],
)
def test_codex_extraction_rejects_paths_links_and_extra_members(tmp_path, members):
    archive = tmp_path / "codex.tar.gz"
    _write_tar(archive, members)

    with pytest.raises(InstallationError, match="Codex archive"):
        extract_codex_binary(archive, tmp_path / "codex")


def test_installer_dry_run_has_no_activation_step():
    plan = dry_run_plan("fixed-model", 2001, 2080)
    encoded = "\n".join(plan).lower()

    assert "sha-256" in encoded
    assert "without package maintainer scripts" in encoded
    assert "codex cli 0.142.5" in encoded
    assert "exact version output" in encoded
    assert "do not enable or start" in encoded
    assert "ssh" not in encoded
    assert "deploy" not in encoded


def test_installer_refuses_to_replace_an_active_unit():
    def runner(argv, *, check=True):
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with pytest.raises(InstallationError, match="active unit"):
        installer._require_units_inactive(runner)


def test_installer_refuses_to_replace_an_active_launcher_instance():
    calls = []

    def runner(argv, *, check=True):
        calls.append(argv)
        returncode = 0 if argv[-1] == "denstock-ai-launcher@*.service" else 3
        return subprocess.CompletedProcess(argv, returncode, stdout=b"", stderr=b"")

    with pytest.raises(InstallationError, match="active launcher instance"):
        installer._require_units_inactive(runner)

    assert calls[-1][-1] == "denstock-ai-launcher@*.service"


def test_existing_codex_is_verified_before_version_execution(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    marker = tmp_path / "codex.sha256"
    binary.write_bytes(b"binary")
    marker.write_text("marker\n", encoding="ascii")
    events = []

    monkeypatch.setattr(installer, "CODEX_BINARY", binary)
    monkeypatch.setattr(installer, "CODEX_MARKER", marker)
    monkeypatch.setattr(
        installer,
        "verify_installed_codex_binary",
        lambda path: events.append(("verify_binary", path)),
    )
    monkeypatch.setattr(
        installer,
        "verify_codex_install_marker",
        lambda path: events.append(("verify_marker", path)),
    )

    def installed_version(_runner):
        events.append(("execute_version", binary))
        return CODEX_CLI_VERSION

    monkeypatch.setattr(installer, "installed_codex_version", installed_version)

    installer._download_and_install_codex()

    assert [event[0] for event in events] == [
        "verify_binary",
        "verify_marker",
        "execute_version",
    ]


def test_launcher_server_config_is_fixed_and_local_only():
    payload = launcher_payload("fixed-model", 2001, 2080)

    assert payload["model"] == "fixed-model"
    assert payload["codex_binary"] == "/usr/local/bin/codex"
    assert payload["codex_cli_version"] == "0.142.5"
    assert payload["proxy_host"] == "127.0.0.1"
    assert payload["proxy_port"] == 2080
    assert payload["request_creator_uid"] == 2001


@pytest.mark.parametrize(
    ("model", "uid", "port"),
    [("bad model", 2001, 2080), ("fixed-model", 0, 2080), ("fixed-model", 2001, 80)],
)
def test_launcher_server_config_rejects_unsafe_admin_values(model, uid, port):
    with pytest.raises(InstallationError):
        launcher_payload(model, uid, port)


def test_rollback_is_idempotent_and_preserves_secrets_and_auth():
    plan = "\n".join(rollback_plan())

    assert "if it remains" in plan
    assert "preserve /etc/denstock-ai, CODEX_HOME and users" in plan
    assert "rm -rf /etc/denstock-ai" not in plan


def test_rollback_refuses_to_remove_firewall_while_launcher_socket_is_active():
    calls = []

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with pytest.raises(RollbackError, match="remains active"):
        apply_rollback(runner)

    assert not any(argv[0] == "/usr/sbin/nft" for argv in calls)


def test_proxy_unit_has_hardening_without_blocking_required_vpn_egress(deploy_root):
    unit = (deploy_root / "systemd" / "denstock-ai-proxy.service").read_text()

    for directive in (
        "User=denstock-ai-proxy",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectKernelLogs=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "MemoryDenyWriteExecute=true",
        "RestrictRealtime=true",
        "SystemCallArchitectures=native",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
    ):
        assert directive in unit
    assert "IPAddressDeny" not in unit
    assert "PrivateNetwork" not in unit
    assert "--config /etc/denstock-ai/sing-box.json" in unit
    assert "ExecStart=/usr/local/lib/denstock-ai/bin/sing-box run" in unit


def test_launcher_unit_adds_cgroup_network_defense_and_hardening(deploy_root):
    unit = (deploy_root / "systemd" / "denstock-ai-launcher@.service").read_text()

    for directive in (
        "User=root",
        "ExecStart=/usr/local/sbin/denstock-ai-launcher socket-serve",
        "StandardInput=socket",
        "StandardOutput=socket",
        "StandardError=null",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "ProtectSystem=strict",
        "NoNewPrivileges=true",
    ):
        assert directive in unit
    assert "PrivateNetwork" not in unit
    assert (
        "CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_KILL CAP_CHOWN CAP_FOWNER "
        "CAP_DAC_OVERRIDE CAP_NET_ADMIN"
    ) in unit
    assert "AmbientCapabilities=" in unit
    for inaccessible in (
        "/opt/denstock",
        "/var/backups",
        "/var/lib/docker",
        "/var/lib/postgresql",
        "/run/docker.sock",
        "/run/postgresql",
    ):
        assert inaccessible in unit


def test_socket_is_local_unix_ipc_with_narrow_group_access(deploy_root):
    unit = (deploy_root / "systemd" / "denstock-ai-launcher.socket").read_text()

    assert "ListenStream=/run/denstock-ai/launcher.sock" in unit
    assert "SocketGroup=denstock-ai-client" in unit
    assert "SocketMode=0660" in unit
    assert "ListenStream=0.0.0.0" not in unit
    assert "ListenDatagram" not in unit


def test_firewall_unit_has_only_required_network_capability(deploy_root):
    unit = (deploy_root / "systemd" / "denstock-ai-firewall.service").read_text()

    assert "CapabilityBoundingSet=CAP_NET_ADMIN CAP_DAC_OVERRIDE" in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_NETLINK" in unit
    assert "RemainAfterExit=true" in unit
    assert "ReadWritePaths=/run/denstock-ai" in unit
    assert "ReadWritePaths=/etc/denstock-ai" not in unit


def test_request_root_mode_allows_ai_traverse_but_not_create():
    assert REQUEST_ROOT_MODE == 0o1731
    assert REQUEST_ROOT_MODE & 0o001
    assert not REQUEST_ROOT_MODE & 0o006


def test_service_user_rejects_forbidden_primary_group(monkeypatch, tmp_path):
    identity = SimpleNamespace(
        pw_shell="/usr/sbin/nologin",
        pw_dir=str(tmp_path),
        pw_gid=999,
    )
    monkeypatch.setattr(
        installer,
        "pwd",
        SimpleNamespace(getpwnam=lambda _name: identity),
    )
    monkeypatch.setattr(
        installer,
        "grp",
        SimpleNamespace(
            getgrall=lambda: [],
            getgrgid=lambda _gid: SimpleNamespace(gr_name="docker"),
        ),
    )

    with pytest.raises(InstallationError, match="forbidden group"):
        installer._ensure_user("denstock-ai", tmp_path)


def test_installer_extracts_only_verified_binary_without_package_scripts(project_root):
    source = (
        project_root
        / "scripts"
        / "ai-support"
        / "denstock_ai_network"
        / "installer.py"
    ).read_text(encoding="utf-8")

    assert '["/usr/bin/dpkg-deb", "--extract"' in source
    assert '["/usr/bin/dpkg", "--install"' not in source
    assert "MAX_PACKAGE_BYTES" in source
    assert "SING_BOX_BINARY" in source


def test_existing_launcher_config_metadata_is_revalidated(project_root):
    source = (
        project_root
        / "scripts"
        / "ai-support"
        / "denstock_ai_network"
        / "installer.py"
    ).read_text(encoding="utf-8")

    assert "not stat.S_ISREG(info.st_mode)" in source
    assert "info.st_uid != 0" in source
    assert "stat.S_IMODE(info.st_mode) != 0o600" in source


def test_no_sudoers_rule_is_installed(deploy_root):
    sudoers_files = [path for path in (deploy_root / "sudoers").rglob("*") if path.is_file()]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sudoers_files)

    assert sudoers_files == [deploy_root / "sudoers" / "README.md"]
    assert "NOPASSWD: ALL" not in text
    assert "does\nnot receive a sudo rule" in text


def test_explicit_lifecycle_wrappers_are_present(deploy_root):
    wrappers = {path.name for path in (deploy_root / "bin").iterdir() if path.is_file()}

    assert {
        "denstock-ai-install",
        "denstock-ai-update",
        "denstock-ai-verify",
        "denstock-ai-rollback",
    } <= wrappers


def test_runtime_verification_contract_is_exact():
    assert EXPECTED_HANDSHAKE == {
        "protocol_version": 1,
        "launcher_version": "1.0.0",
        "codex_cli_version": "0.142.5",
        "network_mode": "maxinik-proxy-only",
        "direct_network_blocked": True,
        "proxy_health": "ok",
    }


def test_installed_verification_checks_codex_before_execution(monkeypatch):
    events = []

    monkeypatch.setattr(verification, "load_launcher_config", lambda _path: object())
    monkeypatch.setattr(verification, "_require_root_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        verification,
        "verify_installed_codex_binary",
        lambda _path: events.append("verify_binary"),
    )
    monkeypatch.setattr(
        verification,
        "verify_codex_install_marker",
        lambda: events.append("verify_marker"),
    )

    def codex_version(_runner):
        events.append("execute_version")
        return CODEX_CLI_VERSION

    monkeypatch.setattr(verification, "installed_codex_version", codex_version)
    monkeypatch.setattr(
        verification,
        "installed_sing_box_version",
        lambda _runner: SING_BOX_VERSION,
    )

    assert verification.verify_installed()["installed"] is True
    assert events == ["verify_binary", "verify_marker", "execute_version"]


def test_runtime_verification_rejects_non_chatgpt_login(monkeypatch):
    monkeypatch.setattr(
        "denstock_ai_network.verification.verify_installed",
        lambda _runner: {"installed": True},
    )

    def runner(argv, *, check=True):
        if "capabilities" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(EXPECTED_HANDSHAKE).encode(), stderr=b""
            )
        return subprocess.CompletedProcess(
            argv, 0, stdout=b"", stderr=b"Logged in using an API key\n"
        )

    with pytest.raises(VerificationError, match="ChatGPT"):
        verification.verify_runtime(runner)


def test_templates_contain_no_real_credentials_or_public_bind(deploy_root):
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in deploy_root.rglob("*")
        if path.is_file()
    )

    assert "vless://" not in text
    assert "0.0.0.0:2080" not in text
    assert "PRIVATE KEY" not in text
    assert "subscription" not in text.lower()
    assert "vpn.invalid" in text
    assert "cover.invalid" in text


def test_static_sing_box_template_is_json_and_has_no_tun(deploy_root):
    template = json.loads((deploy_root / "sing-box.template.json").read_text())
    encoded = json.dumps(template).lower()

    assert template["inbounds"][0]["type"] == "mixed"
    assert template["inbounds"][0]["listen"] == "${MAXINIK_LOCAL_PROXY_HOST}"
    assert '"type": "tun"' not in encoded
    assert "set_system_proxy" in encoded
    assert "default_route" not in encoded


def test_external_compose_override_mounts_only_launcher_ipc(deploy_root):
    compose = (deploy_root / "docker-compose.external.yml").read_text(encoding="utf-8")

    assert "/run/denstock-ai/launcher.sock" in compose
    assert "/var/lib/denstock-ai/requests" in compose
    assert "DENSTOCK_WEB_UID" in compose
    assert "DENSTOCK_AI_CLIENT_GID" in compose
    assert compose.count("create_host_path: false") == 2
    for forbidden in (
        "/etc/denstock-ai",
        "/var/lib/denstock-ai/codex-home",
        "/run/docker.sock",
        "/var/lib/postgresql",
    ):
        assert forbidden not in compose


def test_install_and_rollback_code_has_no_shell_or_ssh(project_root):
    root = project_root / "scripts" / "ai-support" / "denstock_ai_network"
    text = (root / "installer.py").read_text() + (root / "rollback.py").read_text()

    assert "shell=True" not in text
    assert "os.system" not in text
    assert '"/usr/bin/ssh"' not in text.lower()
    assert '"/usr/bin/docker"' not in text.lower()
