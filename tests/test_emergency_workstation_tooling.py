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
    assert (
        "DENSTOCK_MANIFEST_PUBLIC_KEY_PATH="
        "/app/.emergency/trusted/production-manifest-ed25519-public.pem" in installer
    )
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
    # Доверенный ключ не подменяется установкой. Раньше это выражалось отказом
    # при повторном запуске; теперь отпечаток сверяется, совпадение продолжает
    # установку, расхождение останавливает её. Замена ключа - отдельная ротация.
    assert "Замена доверенного ключа" in installer
    assert "Copy-Item -LiteralPath $ManifestPublicKeyPath" in installer


INSTALLER_PATH = ROOT / "scripts" / "operations" / "Install-DenisStock-EmergencyWorkstation.ps1"
INSTALLER = INSTALLER_PATH.read_text(encoding="utf-8")

PRODUCTION_FINGERPRINT = "5615837ef355d2d1881508434980efac31f1c467acb3d31c57101ced3ee5d5b1"


def test_the_pinned_public_key_is_checked_by_fingerprint():
    """Подменённый публичный ключ обязан остановить установку.

    Раньше установщик копировал в доверенные любой переданный файл. Станция,
    закрепившая чужой ключ, приняла бы чужой подписанный снимок за настоящий.
    """
    assert "Get-PublicKeyFingerprint" in INSTALLER
    assert PRODUCTION_FINGERPRINT in INSTALLER, "отпечаток production не закреплён в установщике"
    assert "$sourceFingerprint -ne $ExpectedPublicKeyFingerprint" in INSTALLER, (
        "отпечаток переданного ключа не сверяется с ожидаемым"
    )
    assert "$pinnedFingerprint -ne $ExpectedPublicKeyFingerprint" in INSTALLER, (
        "уже закреплённый ключ не сверяется при повторном запуске"
    )


def test_the_fingerprint_is_computed_from_the_der_public_key():
    """Та же величина, что даёт openssl на сервере, иначе сверять нечего."""
    assert "SHA256" in INSTALLER
    assert "FromBase64String" in INSTALLER
    assert "BEGIN PUBLIC KEY" in INSTALLER


def test_a_repeated_install_keeps_the_station_secrets():
    """Повторный запуск не должен разорвать станции доступ к собственной базе.

    Пароль базы и ключ Django генерируются один раз. Перезапись сделала бы
    рабочую станцию неработоспособной, а прежняя версия просто падала.
    """
    assert '$envExisted = Test-Path -LiteralPath $envFile' in INSTALLER
    assert '.env.emergency уже существует' not in INSTALLER, (
        "повторный запуск снова прерывается вместо продолжения"
    )
    assert "секреты сохранены без изменений" in INSTALLER


def test_a_repeated_install_keeps_the_workstation_identity():
    """UUID станции создаётся один раз: по нему production выдаёт авторизацию."""
    assert "Существующий workstation UUID повреждён" in INSTALLER
    assert "$workstationId = [Guid]::NewGuid().ToString()" in INSTALLER
    identity_block = INSTALLER.split("$identityFile = Join-Path")[1].split("$trustedKeyDir")[0]
    assert "if (Test-Path -LiteralPath $identityFile)" in identity_block, (
        "новый UUID может быть создан поверх существующего"
    )


def test_the_installer_never_carries_private_key_material():
    """В комплекте установки не должно быть приватного ключа ни в каком виде."""
    for forbidden in ("BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY", "production-ed25519.key"):
        assert forbidden not in INSTALLER, f"установщик ссылается на приватный ключ: {forbidden}"


PREFLIGHT_PATH = ROOT / "scripts" / "operations" / "Test-DenisStockEmergencyPreflight.ps1"
PREFLIGHT = PREFLIGHT_PATH.read_text(encoding="utf-8-sig")


def test_the_preflight_only_reads_and_never_changes_the_computer():
    """Проверка перед установкой обязана быть безопасной на чужом компьютере."""
    for forbidden in (
        "New-Item", "Remove-Item", "Set-Content", "Out-File", "New-NetFirewallRule",
        "Register-ScheduledTask", "Set-Acl", "wsl.exe --install", "winget install",
        "Stop-Service", "Start-Service", "Set-ExecutionPolicy",
    ):
        assert forbidden not in PREFLIGHT, f"проверка изменяет систему: {forbidden}"


def test_the_preflight_speaks_in_three_states():
    for state in ("ГОТОВО", "ВНИМАНИЕ", "ОСТАНОВКА"):
        assert state in PREFLIGHT
    assert "exit 0" in PREFLIGHT and "exit 1" in PREFLIGHT


def test_the_preflight_covers_the_blocking_prerequisites():
    """Ровно то, из-за чего установка встанет на месте у компьютера."""
    for probe in (
        "IsInRole", "BuildNumber", "Is64BitOperatingSystem", "VirtualizationFirmwareEnabled",
        "wsl.exe", "com.docker.service", "Win32_PhysicalMemory", "FreeSpace",
        "Get-NetIPAddress", "Get-NetTCPConnection", "workstation-id.txt", "W32Time",
    ):
        assert probe in PREFLIGHT, f"проверка не покрывает: {probe}"


def test_the_preflight_reads_the_wsl_list_in_its_own_encoding():
    """wsl.exe печатает в UTF-16: иначе текст его ошибки станет «дистрибутивом».

    Раньше кодировка задавалась прямо здесь. Теперь тем же занят общий
    помощник, который заодно ограничивает вызов по времени, поэтому проверка
    смотрит на его ключ и на разбор кода возврата.
    """
    assert "-Utf16Output" in PREFLIGHT
    assert "$listing.ExitCode -ne 0" in PREFLIGHT, "ошибка WSL принимается за список дистрибутивов"
    helper = (ROOT / "scripts" / "operations" / "EmergencyBackupSource.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "[Text.Encoding]::Unicode" in helper


def test_the_preflight_ignores_virtual_adapters():
    """Адрес WSL или VPN не является сетью склада.

    Предложенный по такому адресу ярлык не открылся бы с другого компьютера.
    """
    assert "vEthernet" in PREFLIGHT and "virtualPattern" in PREFLIGHT
    assert "виртуальные пропущены" in PREFLIGHT


def test_the_preflight_warns_about_an_address_that_can_change():
    assert "Dhcp" in PREFLIGHT
    assert "Закрепите постоянный адрес" in PREFLIGHT


def test_ram_capacity_uses_installed_physical_modules():
    assert "Win32_PhysicalMemory" in PREFLIGHT
    assert "TotalPhysicalMemory" not in PREFLIGHT
    assert "Win32_PhysicalMemory" in INSTALLER
    assert "TotalPhysicalMemory" not in INSTALLER