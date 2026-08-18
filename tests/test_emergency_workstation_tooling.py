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
    assert "ManifestPublicKeyPath" in installer
    assert "DENSTOCK_MANIFEST_PUBLIC_KEY_PATH=/app/.emergency/trusted/production-manifest-ed25519-public.pem" in installer
    assert "workstation-id.txt" in installer
    assert "DENSTOCK_EMERGENCY_WORKSTATION_ID_PATH=/app/.emergency/workstation-id.txt" in installer


def test_operator_launcher_blocks_secondary_and_supports_wsl_without_docker_desktop():
    launcher = (ROOT / "scripts" / "operations" / "DenisStock-Emergency.ps1").read_text(
        encoding="utf-8"
    )

    assert '"wsl2"' in launcher
    assert "cold standby secondary" in launcher
    assert "Docker Desktop для emergency runtime не требуется" in launcher


def test_launcher_updates_exact_release_for_a_new_backup_and_rolls_back_on_failure():
    launcher = (ROOT / "scripts" / "operations" / "DenisStock-Emergency.ps1").read_text(
        encoding="utf-8"
    )

    assert "DENSTOCK_EMERGENCY_RELEASE_SOURCE" in launcher
    assert "git fetch --no-tags $source $target" in launcher
    assert "Restore-PreviousRelease" in launcher
    assert "$checkoutChanged = $false" in launcher
    assert 'git checkout --detach $current' in launcher
    assert "standby-refresh-status.json" in launcher
    assert "Show-StandbyRefreshWarning" in launcher


def test_provisioning_keeps_secrets_and_backup_runtime_off_regular_desktops():
    installer = (
        ROOT / "scripts" / "operations" / "Install-DenisStock-EmergencyWorkstation.ps1"
    ).read_text(encoding="utf-8")

    assert "Set-AdministratorOnlyAcl" in installer
    assert 'Join-Path $RepoRoot ".emergency"' in installer
    assert '[Environment]::GetFolderPath("Desktop")' in installer
    assert "CommonDesktopDirectory" not in installer
    assert "WSL systemd включён" in installer
    assert "Pinned production public key уже существует" in installer
