from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_emergency_proxy_is_loopback_by_default_but_can_use_a_configured_lan_primary():
    compose = (ROOT / "docker-compose.emergency.yml").read_text(encoding="utf-8")

    assert '${DENSTOCK_EMERGENCY_BIND_HOST:-127.0.0.1}' in compose


def test_workstation_provisioner_uses_wsl_engine_and_scoped_lan_firewall():
    installer = (
        ROOT / "scripts" / "operations" / "Install-DenisStock-EmergencyWorkstation.ps1"
    ).read_text(encoding="utf-8")

    assert "WSL2 Docker Engine" in installer
    assert "Rclone.Rclone" in installer
    assert "-RemoteAddress LocalSubnet" in installer
    assert "DENSTOCK_EMERGENCY_ROLE=$Role" in installer
    assert "DENSTOCK_EMERGENCY_BIND_HOST=$bindHost" in installer
    assert "POSTGRES_PASSWORD=$postgresPassword" in installer


def test_operator_launcher_blocks_secondary_and_supports_wsl_without_docker_desktop():
    launcher = (ROOT / "scripts" / "operations" / "DenisStock-Emergency.ps1").read_text(
        encoding="utf-8"
    )

    assert '"wsl2"' in launcher
    assert "cold standby secondary" in launcher
    assert "Docker Desktop для emergency runtime не требуется" in launcher
