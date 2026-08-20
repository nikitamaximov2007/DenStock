"""Откуда обновление копии берёт настройки rclone и кто их может прочитать.

Две отдельные беды, обе тихие.

Первая: задание обновления идёт с типом входа S4U, и полагаться на то, что
Windows подставит профиль и переменную APPDATA, нельзя. Если не подставит,
rclone не найдёт свои настройки, источник копий "исчезнет", и станция начнёт
устаревать без единой ошибки. Поэтому путь определяется при установке и
задаётся явно.

Вторая: в этих настройках лежит ключ доступа к хранилищу копий, а файл по
умолчанию наследует права профиля. На живой Windows его читали группы
«Пользователи» и «Прошедшие проверку», то есть любая учётная запись компьютера.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "operations"
HELPER = OPS / "EmergencyBackupSource.ps1"
INSTALLER = (OPS / "Install-DenisStock-EmergencyWorkstation.ps1").read_text(encoding="utf-8-sig")
WRAPPER = (OPS / "Emergency-Standby-Refresh.ps1").read_text(encoding="utf-8-sig")
PROTECT = (OPS / "Protect-DenisStockEmergencyCredentials.ps1").read_text(encoding="utf-8-sig")
SOURCE = HELPER.read_text(encoding="utf-8-sig")

POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="нужен Windows PowerShell")


def run_powershell(body: str, timeout: int = 180) -> str:
    prelude = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$OutputEncoding = [Text.Encoding]::UTF8; "
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         prelude + f". '{HELPER}'\n{body}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    assert result.returncode == 0, f"PowerShell завершился с ошибкой: {result.stderr}"
    return result.stdout.strip()


# --- Откуда берётся путь --------------------------------------------------------


def test_the_installer_records_the_config_path():
    """Иначе задание зависело бы от того, подставит ли Windows профиль."""
    assert "DENSTOCK_EMERGENCY_RCLONE_CONFIG=$rcloneConfigPath" in INSTALLER
    assert "$env:RCLONE_CONFIG" in INSTALLER


def test_the_installer_refuses_to_finish_without_rclone_settings():
    """Станция без настроек встала бы готовой и не смогла забрать копию."""
    assert "Не найдены настройки rclone" in INSTALLER
    assert "rclone config под этой же учётной записью" in INSTALLER


def test_the_refresh_wrapper_sets_the_config_path_explicitly():
    assert "$env:RCLONE_CONFIG = $configured" in WRAPPER
    assert "DENSTOCK_EMERGENCY_RCLONE_CONFIG" in WRAPPER
    assert "S4U" in WRAPPER, "не объяснено, почему путь задаётся явно"


def test_the_wrapper_never_carries_a_secret():
    for forbidden in ("access_key", "secret_access_key", "PROBE_TOKEN", "PASSWORD"):
        assert forbidden not in WRAPPER, f"обёртка обновления несёт секрет: {forbidden}"


@needs_powershell
def test_the_wrapper_injects_the_recorded_path(tmp_path):
    """Проверяется поведением: подставляется весь путь целиком."""
    station = tmp_path / "station"
    (station / "scripts" / "operations").mkdir(parents=True)
    (station / ".env.emergency").write_text(
        "DENSTOCK_EMERGENCY_RCLONE_CONFIG=C:\\Custom Folder\\rclone.conf\n", encoding="utf-8"
    )
    wrapper = station / "scripts" / "operations" / "Emergency-Standby-Refresh.ps1"
    wrapper.write_bytes((OPS / "Emergency-Standby-Refresh.ps1").read_bytes())
    stub = station / "scripts" / "operations" / "DenisStock-Emergency.ps1"
    stub.write_bytes(
        b"\xef\xbb\xbf"
        + b'param([string]$Action, [switch]$NonInteractive)\n'
        + b'Write-Output ("PATH=" + $env:RCLONE_CONFIG)\nexit 0\n'
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert "PATH=C:\\Custom Folder\\rclone.conf" in result.stdout, result.stdout


@needs_powershell
def test_a_station_without_the_recorded_path_still_runs(tmp_path):
    """Станции, поставленные до этой правки, не должны сломаться."""
    station = tmp_path / "old"
    (station / "scripts" / "operations").mkdir(parents=True)
    (station / ".env.emergency").write_text("DENSTOCK_MODE=emergency-local\n", encoding="utf-8")
    wrapper = station / "scripts" / "operations" / "Emergency-Standby-Refresh.ps1"
    wrapper.write_bytes((OPS / "Emergency-Standby-Refresh.ps1").read_bytes())
    stub = station / "scripts" / "operations" / "DenisStock-Emergency.ps1"
    stub.write_bytes(
        b"\xef\xbb\xbf"
        + b'param([string]$Action, [switch]$NonInteractive)\nWrite-Output "RAN"\nexit 0\n'
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert "RAN" in result.stdout, result.stdout


# --- Кто может прочитать ключ ------------------------------------------------------


def test_the_hardening_is_an_explicit_command_not_a_side_effect():
    """Слишком узкие права сломали бы обновление молча, поэтому решает человек."""
    assert "Protect-RcloneConfig" not in INSTALLER, "права меняются сами при установке"
    assert (OPS / "Protect-DenisStockEmergencyCredentials.ps1").is_file()
    assert "-WhatIf" in PROTECT


def test_the_hardening_keeps_the_four_who_need_access():
    assert "S-1-5-18" in SOURCE, "нет СИСТЕМЫ"
    assert "S-1-5-32-544" in SOURCE, "нет администраторов"
    assert "$TaskAccount" in SOURCE, "нет учётной записи задания"
    assert "WindowsIdentity]::GetCurrent().Name" in SOURCE, "нет владельца"


def test_the_system_accounts_are_taken_by_identifier_not_by_name():
    """На русской Windows они называются иначе."""
    assert "SecurityIdentifier" in SOURCE
    assert "NT AUTHORITY\\SYSTEM" not in SOURCE.replace("expected", "")


def test_the_hardening_never_reads_the_file():
    body = "\n".join(
        line for line in SOURCE.splitlines() if not line.strip().startswith("#")
    )
    block = body.split("function Protect-RcloneConfig")[1]
    for forbidden in ("Get-Content", "Select-String", "ConvertFrom-StringData"):
        assert forbidden not in block, f"ограничение прав читает файл: {forbidden}"


@needs_powershell
def test_hardening_removes_the_broad_groups_and_keeps_the_owner(tmp_path):
    """Проверяется на настоящем файле: до правки его читали любые пользователи."""
    target = tmp_path / "rclone.conf"
    target.write_text("[yandex-s3]\n", encoding="utf-8")
    output = run_powershell(
        f'$r = Protect-RcloneConfig -Path "{target}"; '
        '"$($r.Changed)|" + (($r.After | Sort-Object) -join ";")'
    )
    changed, after = output.split("|", 1)
    assert changed == "True"
    readers = [item for item in after.split(";") if item]
    assert len(readers) <= 4, f"осталось слишком много читателей: {readers}"
    lowered = " ".join(readers).lower()
    assert "user" in lowered or "админ" in lowered, f"владелец потерял доступ: {readers}"
    assert target.read_text(encoding="utf-8") == "[yandex-s3]\n", "файл изменён"


@needs_powershell
def test_hardening_is_safe_to_repeat(tmp_path):
    """Повтор не должен ничего писать: перезапись требует особой привилегии."""
    target = tmp_path / "rclone.conf"
    target.write_text("[yandex-s3]\n", encoding="utf-8")
    first = run_powershell(f'(Protect-RcloneConfig -Path "{target}").Changed')
    second = run_powershell(f'(Protect-RcloneConfig -Path "{target}").Changed')
    assert first == "True"
    assert second == "False", "повторный запуск снова переписывает права"


@needs_powershell
def test_the_preview_changes_nothing(tmp_path):
    target = tmp_path / "rclone.conf"
    target.write_text("[yandex-s3]\n", encoding="utf-8")
    output = run_powershell(f'(Protect-RcloneConfig -Path "{target}" -WhatIf).Changed')
    assert output == "False"
    protected = run_powershell(f'(Get-Acl -LiteralPath "{target}").AreAccessRulesProtected')
    assert protected == "False", "предварительный показ изменил права"


@needs_powershell
def test_a_missing_file_is_reported_clearly(tmp_path):
    output = run_powershell(
        f'try {{ Protect-RcloneConfig -Path "{tmp_path / "нет.conf"}" | Out-Null; "NO ERROR" }} '
        'catch { $_.Exception.Message }'
    )
    assert "не найден" in output, output
