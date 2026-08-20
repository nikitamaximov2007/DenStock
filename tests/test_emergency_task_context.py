"""Контекст, в котором завтра будет работать обновление копии.

Самая дорогая ошибка здесь тихая. Обновление выполняет rclone, а его настройки
лежат в профиле того пользователя Windows, который их создавал. Задание под
системной учётной записью получило бы другой профиль, источник копий не нашёлся
бы, и станция начала бы устаревать без единой ошибки на экране.

Вторая тихая беда - время. Внешние вызовы без ограничения превращали проверку
готовности в молчащее окно: на машине со сломанной подсистемой Linux вызов
wsl.exe возвращался четыре минуты.

Часть проверок запускает настоящий PowerShell, потому что поведение по времени
чтением исходника не подтверждается.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "operations"
HELPER = OPS / "EmergencyBackupSource.ps1"
INSTALLER = (OPS / "Install-DenisStock-EmergencyWorkstation.ps1").read_text(encoding="utf-8-sig")
DIAGNOSTICS = (OPS / "Test-DenisStockEmergency.ps1").read_text(encoding="utf-8-sig")
PREFLIGHT = (OPS / "Test-DenisStockEmergencyPreflight.ps1").read_text(encoding="utf-8-sig")
SOURCE = HELPER.read_text(encoding="utf-8-sig")

POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="нужен Windows PowerShell")


def run_powershell(body: str, timeout: int = 240) -> str:
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


# --- Учётная запись задания -------------------------------------------------------


def test_the_refresh_task_declares_its_account_explicitly():
    """Умолчание Windows здесь не годится: от него зависит, найдётся ли источник."""
    assert "New-ScheduledTaskPrincipal" in INSTALLER
    assert "-Principal $principal" in INSTALLER
    assert "WindowsIdentity]::GetCurrent().Name" in INSTALLER


def test_the_refresh_task_runs_without_a_logged_on_user():
    """Компьютер склада стоит с заблокированным экраном.

    При типе входа Interactive ежедневные запуски в 07:00 и 19:00 не сработали
    бы, и копия старела бы неделями.
    """
    assert "-LogonType S4U" in INSTALLER
    assert "-LogonType Interactive" not in INSTALLER


def test_the_refresh_task_never_runs_as_the_system_account():
    """У системной учётной записи другой профиль и нет настроек rclone."""
    for forbidden in ("NT AUTHORITY\\SYSTEM", "-UserId SYSTEM", "ServiceAccount"):
        assert forbidden not in INSTALLER, f"задание может пойти под системой: {forbidden}"


def test_a_missed_daily_run_is_caught_up():
    """Ночью компьютер может быть выключен."""
    assert "-StartWhenAvailable" in INSTALLER


def test_the_task_does_not_pile_up_instances():
    assert "-MultipleInstances IgnoreNew" in INSTALLER
    assert "-ExecutionTimeLimit" in INSTALLER


def test_the_task_carries_no_secret_in_its_arguments():
    """Аргументы задания видны любому, кто откроет планировщик."""
    block = INSTALLER.split("if ($CreateTasks)")[1]
    for forbidden in ("$probeValue", "$postgresPassword", "access_key", "secret",
                      "-Password", "RCLONE_CONFIG"):
        assert forbidden not in block, f"в аргументах задания секрет: {forbidden}"


def test_the_readiness_reports_the_task_context():
    assert "Контекст задания" in DIAGNOSTICS
    assert "Principal.UserId" in DIAGNOSTICS or "$refreshTask.Principal.UserId" in DIAGNOSTICS
    assert "у неё другой профиль" in DIAGNOSTICS


def test_the_readiness_warns_about_an_interactive_task():
    assert '$logon -eq "Interactive"' in DIAGNOSTICS
    assert "заблокированном экране" in DIAGNOSTICS


# --- Ограничение по времени ---------------------------------------------------------


def test_the_helper_can_bound_an_external_call():
    assert "function Invoke-ExternalWithTimeout" in SOURCE
    assert "WaitForExit" in SOURCE
    assert "$process.Kill()" in SOURCE


def test_the_wsl_and_docker_calls_are_bounded():
    """Именно они висели: проверено замером, 241 секунда на сломанной машине."""
    for script, name in ((DIAGNOSTICS, "диагностика"), (PREFLIGHT, "проверка перед установкой")):
        assert "Invoke-ExternalWithTimeout" in script, f"{name}: вызов не ограничен"
        assert "-TimeoutSeconds" in script
    assert "& wsl.exe -l -q" not in DIAGNOSTICS, "остался неограниченный вызов"
    assert "& wsl.exe -l -q" not in PREFLIGHT, "остался неограниченный вызов"
    assert "& wsl.exe -d $WslDistro -- docker info" not in DIAGNOSTICS


def test_a_timeout_is_its_own_state_with_an_action():
    assert "не ответил за 25 секунд" in DIAGNOSTICS
    assert "wsl --shutdown" in DIAGNOSTICS


@needs_powershell
def test_a_slow_command_is_actually_killed():
    """Поведение по времени чтением исходника не подтверждается."""
    output = run_powershell(
        '$sw = [Diagnostics.Stopwatch]::StartNew(); '
        '$r = Invoke-ExternalWithTimeout -FilePath "ping.exe" '
        '-Arguments @("-n","30","127.0.0.1") -TimeoutSeconds 3; '
        '$sw.Stop(); "$($r.TimedOut)|$($sw.Elapsed.TotalSeconds -lt 15)"'
    )
    assert output == "True|True", f"медленная команда не снята вовремя: {output}"


@needs_powershell
def test_a_fast_command_returns_its_output_and_code():
    output = run_powershell(
        '$r = Invoke-ExternalWithTimeout -FilePath "cmd.exe" '
        '-Arguments @("/c","exit","3") -TimeoutSeconds 20; '
        '"$($r.ExitCode)|$($r.TimedOut)"'
    )
    assert output == "3|False"


# --- Настройки rclone ----------------------------------------------------------------


@needs_powershell
def test_the_config_path_follows_the_windows_convention():
    """Проверено на живой Windows: файл лежит в профиле пользователя."""
    output = run_powershell('(Get-RcloneConfigPath).Path')
    assert output.endswith("rclone.conf")
    assert "AppData" in output or "RCLONE_CONFIG" in SOURCE


def test_the_config_path_is_computed_and_not_asked_from_rclone():
    """Подкоманда config не входит в список разрешённых намеренно."""
    assert "APPDATA" in SOURCE
    assert "RCLONE_CONFIG" in SOURCE
    assert '"config"' not in SOURCE


@needs_powershell
def test_the_config_contents_are_never_returned():
    output = run_powershell(
        '$c = Get-RcloneConfigPath; ($c.PSObject.Properties.Name | Sort-Object) -join ","'
    )
    fields = set(output.split(","))
    assert fields == {"AclProtected", "Exists", "ExtraReaders", "Path", "Source"}, output
    for forbidden in ("Content", "Text", "Secret", "Key"):
        assert forbidden not in fields


def test_extra_readers_of_the_credentials_are_reported():
    """В файле лежит ключ к хранилищу копий."""
    assert "ExtraReaders" in SOURCE
    assert "лишние читатели" in DIAGNOSTICS


# --- Источник выпуска ------------------------------------------------------------------


def test_the_release_source_is_checked_read_only():
    """Проверка не публикует ветку и ничего не скачивает."""
    assert "Test-EmergencyReleaseSource" in SOURCE
    assert "ls-remote" in SOURCE
    assert "--dry-run" in SOURCE
    for forbidden in ("git push", "git checkout", "git reset", "git commit"):
        assert forbidden not in SOURCE, f"проверка выпуска меняет репозиторий: {forbidden}"


def test_the_release_check_requires_a_full_sha():
    """Короткий SHA принял бы не тот коммит."""
    assert '"^[0-9a-f]{40}$"' in SOURCE


@needs_powershell
def test_a_missing_commit_is_told_apart_from_a_missing_source():
    """Ровно этот случай и подвёл: ветка выглядела опубликованной, а её не было."""
    repo = str(ROOT)
    missing_commit = run_powershell(
        f'(Test-EmergencyReleaseSource -Source "origin" -Commit "{"0" * 39}1" '
        f'-RepoRoot "{repo}" -TimeoutSeconds 60).Kind',
        timeout=240,
    )
    assert missing_commit == "commit-missing", missing_commit

    bad_source = run_powershell(
        '(Test-EmergencyReleaseSource -Source "https://example.invalid/x.git" '
        f'-Commit "{"0" * 40}" -RepoRoot "{repo}" -TimeoutSeconds 25).Kind',
        timeout=240,
    )
    assert bad_source in {"unreachable", "source-missing", "timeout", "auth"}, bad_source


@needs_powershell
def test_an_empty_release_source_does_not_crash_the_check():
    kind = run_powershell('(Test-EmergencyReleaseSource -Source "" -Commit "").Kind')
    assert kind == "not-configured"


def test_the_readiness_reports_the_release_source():
    assert "Источник выпуска" in DIAGNOSTICS
    assert "DENSTOCK_EMERGENCY_RELEASE_SOURCE" in DIAGNOSTICS


def test_the_release_update_still_fails_closed():
    """Существующие гарантии обновления версии не должны ослабнуть."""
    launcher = (OPS / "DenisStock-Emergency.ps1").read_text(encoding="utf-8-sig")
    assert "Нельзя менять application release из dirty checkout." in launcher
    assert 'git fetch --no-tags $source $target' in launcher.replace("& ", "")
    assert '$resolved -ne $target' in launcher, "точное совпадение коммита не проверяется"
    assert "Старый READY standby сохранён." in launcher


# --- Совместимость с Windows 10 и подсистемой Linux -------------------------------------

BOOTSTRAP = (OPS / "provision-wsl-docker.sh").read_text(encoding="utf-8")


def test_the_bootstrap_does_not_loop_when_systemd_is_unsupported():
    """Встроенный WSL Windows 10 systemd не умеет.

    Раньше сценарий просто прописывал systemd и просил перезапуск. При
    неподдерживающей версии человек получал то же сообщение снова и снова.
    """
    assert "grep -qs '^systemd=true' /etc/wsl.conf" in BOOTSTRAP
    assert "exit 43" in BOOTSTRAP
    assert "wsl --update" in BOOTSTRAP
    assert "Microsoft Store" in BOOTSTRAP


def test_the_bootstrap_is_safe_to_repeat():
    """Повторный запуск не должен переустанавливать уже стоящий Docker."""
    assert "command -v docker" in BOOTSTRAP
    assert "systemctl enable --now docker" in BOOTSTRAP


def test_the_preflight_checks_the_wsl_version():
    """Проверено на живой машине: современный WSL отвечает на --version."""
    assert '"--version"' in PREFLIGHT
    assert "systemd" in PREFLIGHT
    assert "Microsoft Store" in PREFLIGHT


def test_windows_home_uses_no_pro_only_feature():
    """Аварийный режим не должен требовать Windows Pro из-за одной команды."""
    installer_and_checks = INSTALLER + PREFLIGHT + DIAGNOSTICS
    for pro_only in ("Enable-WindowsOptionalFeature -FeatureName Microsoft-Hyper-V",
                     "Hyper-V-All", "Get-VM", "New-VM", "gpedit", "BitLocker",
                     "Get-WindowsFeature"):
        assert pro_only not in installer_and_checks, f"нужна была бы Windows Pro: {pro_only}"


def test_only_home_available_windows_apis_are_used():
    """Всё перечисленное есть в Windows 10 Home."""
    for api in ("New-NetFirewallRule", "Register-ScheduledTask", "Get-Acl", "Set-Acl",
                "WScript.Shell", "Get-CimInstance"):
        assert api in INSTALLER, f"проверка бессмысленна: {api} больше не используется"
