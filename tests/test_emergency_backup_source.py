"""Проверка источника резервных копий аварийной станции.

Установка станции опирается на источник копий, но проверить его до установки
было нечем: отказ обнаруживался на первой синхронизации и выглядел одинаково
для любой причины.

Здесь закреплены два свойства, которые дороже всего стоят на месте у
компьютера: проверка никогда не пишет в хранилище, и она не печатает учётные
данные. Плюс то, что род отказа определяется правильно, иначе человек получит
подсказку не про свою беду.

Часть проверок запускает настоящий PowerShell: разбор кода и текста ошибки
нельзя подтвердить чтением исходника. Там, где PowerShell недоступен, такие
проверки пропускаются.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "operations"
HELPER = OPS / "EmergencyBackupSource.ps1"
PREFLIGHT = (OPS / "Test-DenisStockEmergencyPreflight.ps1").read_text(encoding="utf-8-sig")
DIAGNOSTICS = (OPS / "Test-DenisStockEmergency.ps1").read_text(encoding="utf-8-sig")
COLLECT = (OPS / "Collect-DenisStockEmergencyDiagnostics.ps1").read_text(encoding="utf-8-sig")
LAUNCHER = (OPS / "DenisStock-Emergency.ps1").read_text(encoding="utf-8-sig")
SOURCE = HELPER.read_text(encoding="utf-8-sig")

POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="нужен Windows PowerShell")


def run_powershell(body: str) -> str:
    """Выполнить фрагмент с подключённым помощником и вернуть вывод."""
    # powershell.exe печатает в кодировке консоли, поэтому её надо переключить
    # явно: иначе русские слова приходят сюда вопросительными знаками. Это тот
    # же род ловушки, что и метка UTF-8 у самих сценариев.
    prelude = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$OutputEncoding = [Text.Encoding]::UTF8; "
    )
    script = prelude + f". '{HELPER}'\n{body}"
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert result.returncode == 0, f"PowerShell завершился с ошибкой: {result.stderr}"
    return result.stdout.strip()


# --- Только чтение --------------------------------------------------------------


def test_the_check_never_writes_to_the_backup_storage():
    """Станция забирает копии и ничего в них не меняет."""
    for forbidden in ("rclone copy", "rclone sync", "rclone delete", "rclone purge",
                      "rclone rcat", "rclone mkdir", "rclone move", "rclone touch"):
        assert forbidden not in SOURCE, f"проверка пишет в хранилище: {forbidden}"


def test_the_allowed_command_list_is_closed_and_read_only():
    assert '$allowed = @("version", "listremotes", "lsjson", "lsf")' in SOURCE
    assert "не разрешена" in SOURCE


@needs_powershell
@pytest.mark.parametrize("command", ["copy", "sync", "delete", "purge", "rcat", "mkdir", "config"])
def test_a_writing_command_is_refused_at_runtime(command):
    """Список закрыт не на словах: запрещённая команда не выполняется."""
    output = run_powershell(
        f'try {{ Invoke-RcloneRead -Arguments @("{command}") | Out-Null; "ALLOWED" }} '
        'catch { "REFUSED" }'
    )
    assert output == "REFUSED", f"команда {command} прошла в проверку источника"


# --- Разбор источника -----------------------------------------------------------


@needs_powershell
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("yandex-s3:denstock-backups-nikita", "rclone-remote"),
        ("yandex-s3:denstock-backups-nikita/sub", "rclone-remote"),
        ("C:\\backups", "windows-path"),
        ("C:/backups", "windows-path"),
        ("D:\\x\\y", "windows-path"),
        ("", "empty"),
    ],
)
def test_source_kind_matches_what_sync_will_do(source, expected):
    """Проверка обязана судить об источнике так же, как рабочая синхронизация.

    Иначе она скажет «готово» там, где синхронизация не пройдёт.
    """
    assert run_powershell(f'Get-BackupSourceKind -Source "{source}"') == expected


def test_the_source_rule_is_copied_from_the_sync_path():
    """Правило в помощнике и в синхронизации должно быть одним и тем же."""
    for rule in ('^[A-Za-z]:[\\\\/]', '^[A-Za-z0-9_.-]+:'):
        assert rule in SOURCE, f"правило разбора разошлось с синхронизацией: {rule}"
        assert rule in LAUNCHER, f"правило синхронизации изменилось: {rule}"


@needs_powershell
def test_the_remote_name_is_split_from_the_path():
    output = run_powershell(
        '$p = Split-RcloneSource -Source "yandex-s3:denstock-backups-nikita/sub"; '
        '"$($p.Remote)|$($p.Path)"'
    )
    assert output == "yandex-s3|denstock-backups-nikita/sub"


# --- Род отказа -----------------------------------------------------------------


@needs_powershell
@pytest.mark.parametrize(
    ("exit_code", "text", "expected"),
    [
        (1, "InvalidAccessKeyId: The Access Key Id you provided does not exist", "auth"),
        (1, "SignatureDoesNotMatch: signature we calculated does not match", "auth"),
        (1, "ExpiredToken: the token has expired", "auth"),
        (1, "AccessDenied: Access Denied", "access-denied"),
        (1, "NoSuchBucket: The specified bucket does not exist", "source-missing"),
        (3, "directory not found", "source-missing"),
        (1, "didn't find section in config file", "remote-missing"),
        (1, "dial tcp: lookup storage.yandexcloud.net: no such host", "network"),
        (1, "x509: certificate signed by unknown authority", "network"),
        (5, "temporary error, retries exceeded", "network"),
        (1, "RequestTimeTooSkewed: the difference between the request time", "clock-skew"),
        (2, "unrecognised trouble", "unknown"),
    ],
)
def test_every_failure_kind_is_told_apart(exit_code, text, expected):
    """Оператору нужна своя подсказка на каждую беду, а не общее «не вышло»."""
    got = run_powershell(f'Get-RcloneFailureKind -ExitCode {exit_code} -ErrorText @"\n{text}\n"@')
    assert got == expected


def test_the_exit_code_is_used_and_not_only_the_message():
    """Код возврата задокументирован, поэтому на него можно опереться."""
    assert "$ExitCode -eq 3" in SOURCE
    assert "$ExitCode -eq 5" in SOURCE


@needs_powershell
@pytest.mark.parametrize(
    "kind",
    ["not-installed", "remote-missing", "auth", "access-denied", "source-missing",
     "network", "clock-skew", "empty", "unknown"],
)
def test_every_failure_kind_has_an_action_for_the_operator(kind):
    advice = run_powershell(f'Get-BackupSourceFailureAdvice -Kind "{kind}" -Remote "yandex-s3"')
    assert len(advice) > 25, f"подсказка для «{kind}» слишком коротка, чтобы помочь"
    assert "Exception" not in advice


# --- Настоящий локальный источник -------------------------------------------------


@needs_powershell
def test_a_local_source_reports_the_latest_complete_run(tmp_path):
    """Незавершённая копия без manifest.json не должна считаться свежей."""
    for name in ("2026-08-19_03-00-22", "2026-08-20_08-40-07"):
        run = tmp_path / name
        run.mkdir()
        (run / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "partial-run").mkdir()

    output = run_powershell(
        f'$r = Test-EmergencyBackupSource -Source "{tmp_path}"; '
        '"$($r.State)|$($r.Kind)|$($r.LatestRun)|$($r.Runs.Count)"'
    )
    state, kind, latest, count = output.split("|")
    assert state == "ГОТОВО"
    assert kind == "ok"
    assert latest == "2026-08-20_08-40-07"
    assert count == "2", "незавершённая копия попала в список"


@needs_powershell
def test_a_local_source_without_runs_is_not_ready(tmp_path):
    output = run_powershell(
        f'$r = Test-EmergencyBackupSource -Source "{tmp_path}"; "$($r.State)|$($r.Kind)"'
    )
    assert output == "ОСТАНОВКА|empty"


@needs_powershell
def test_a_missing_local_source_is_told_apart_from_an_empty_one(tmp_path):
    missing = tmp_path / "нет-такого"
    output = run_powershell(
        f'$r = Test-EmergencyBackupSource -Source "{missing}"; "$($r.Kind)"'
    )
    assert output == "source-missing"


@needs_powershell
def test_the_backup_age_comes_from_the_run_name():
    """Имя задаёт production при создании копии, а время файла меняет выгрузка."""
    assert run_powershell('Get-BackupRunAgeHours -RunId "не-дата" -eq $null; '
                          '(Get-BackupRunAgeHours -RunId "не-дата") -eq $null') == "True"
    hours = run_powershell('Get-BackupRunAgeHours -RunId "2020-01-01_00-00-00"')
    assert float(hours.replace(",", ".")) > 0


# --- Секреты ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [HELPER, OPS / "Test-DenisStockEmergencyPreflight.ps1",
     OPS / "Test-DenisStockEmergency.ps1", OPS / "Collect-DenisStockEmergencyDiagnostics.ps1"],
    ids=lambda p: p.name,
)
def test_no_script_reads_or_prints_the_rclone_configuration(script):
    """В rclone.conf лежат ключ доступа и секретный ключ."""
    text = script.read_text(encoding="utf-8-sig")
    # Ищется обращение, а не упоминание: сценарии прямо пишут в комментариях,
    # что настройки не читаются, и запрет на слово запрещал бы объяснение.
    lines = [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    body = "\n".join(lines)
    # Совет «настройте источник командой rclone config» адресован человеку и
    # обращением к настройкам не является. Ищется именно вызов.
    for forbidden in ("& rclone config", "rclone.exe config", "config dump", "config show"):
        assert forbidden not in body, f"{script.name} читает настройки rclone: {forbidden}"
    assert '"config"' not in body, f"{script.name} допускает подкоманду config"
    for line in lines:
        if "Copy-Item" in line or "Get-Content" in line:
            assert "rclone" not in line.lower(), f"{script.name}: {line.strip()}"


def test_the_diagnostics_bundle_denies_storage_credentials():
    for pattern in ("access_key_id", "secret_access_key", "AKIA"):
        assert pattern in COLLECT, f"в списке запретов нет {pattern}"
    assert "config_contents=<не собирается никогда>" in COLLECT


def test_the_bundle_never_copies_the_rclone_configuration():
    assert "Copy-Item" in COLLECT, "проверка бессмысленна: копирования в сборщике нет вовсе"
    copied = [line for line in COLLECT.splitlines() if "Copy-Item" in line]
    for line in copied:
        assert "rclone" not in line.lower(), f"настройки rclone копируются в архив: {line.strip()}"


def test_credentials_are_never_taken_as_a_parameter():
    """Секрет, переданный параметром, попадает в журнал команд и в историю."""
    for script in (SOURCE, PREFLIGHT, DIAGNOSTICS, COLLECT):
        for forbidden in ("$AccessKey", "$SecretKey", "$AccessKeyId", "$SecretAccessKey"):
            assert forbidden not in script, f"учётные данные принимаются параметром: {forbidden}"


# --- Встроенность в проверки --------------------------------------------------------


def test_the_preflight_can_check_the_source_before_install():
    assert "$BackupSource" in PREFLIGHT
    assert "Test-EmergencyBackupSource" in PREFLIGHT
    assert "станция встанет готовой" in PREFLIGHT


def test_the_readiness_command_reports_the_source_and_its_freshness():
    assert "Test-EmergencyBackupSource" in DIAGNOSTICS
    assert "Свежесть копии" in DIAGNOSTICS
    assert "DENSTOCK_EMERGENCY_STALE_WARNING_HOURS" in DIAGNOSTICS


def test_the_readiness_command_tells_the_operator_the_next_step():
    """После перезагрузки человек должен видеть, на каком шаге он стоит."""
    assert "Что делать дальше:" in DIAGNOSTICS
    assert "Add-NextStep" in DIAGNOSTICS
    assert "станция ещё не установлена" in DIAGNOSTICS


def test_a_not_yet_installed_station_is_not_reported_as_broken():
    """Иначе после первого запуска человек видит красный список и думает, что всё сломано."""
    assert "это не поломка, а" in DIAGNOSTICS
    assert 'НЕ ГОТОВО: станция ещё не установлена' in DIAGNOSTICS


def test_the_helper_is_shared_and_not_duplicated():
    """Одно правило на две проверки: расхождение опаснее отсутствия проверки."""
    for script in (PREFLIGHT, DIAGNOSTICS, COLLECT):
        assert "EmergencyBackupSource.ps1" in script


def test_the_json_listing_is_preferred_over_text_parsing():
    assert "lsjson" in SOURCE
    assert "ConvertFrom-Json" in SOURCE


def test_the_probe_is_bounded_in_time():
    """Проверка не должна висеть у человека на экране без предела."""
    for flag in ("--contimeout", "--timeout", "--retries"):
        assert flag in SOURCE, f"нет ограничения {flag}"


def test_json_listing_flags_are_valid_rclone_flags():
    """Опечатка во флаге превратила бы читающую проверку в ошибку разбора."""
    known = {"--dirs-only", "--no-modtime", "--contimeout", "--timeout",
             "--retries", "--low-level-retries"}
    used = {
        word.strip('",')
        for word in SOURCE.split()
        if word.startswith("--") and word.strip('",')[2:3].isalpha()
    }
    unknown = {flag for flag in used if flag not in known}
    assert not unknown, f"неизвестные флаги rclone: {unknown}"


def test_the_helper_result_shape_is_stable():
    """Поля читают оба вызывающих сценария, переименование сломает их молча."""
    for field in ("State", "Kind", "Detail", "Advice", "Remote", "LatestRun", "Runs",
                  "RcloneVersion"):
        assert field in SOURCE
    for field in ("State", "Kind", "Detail", "Advice", "LatestRun", "RcloneVersion"):
        assert f"$probe.{field}" in PREFLIGHT + DIAGNOSTICS, f"поле {field} нигде не читается"


def test_the_manifest_signature_contract_is_untouched():
    """Проверка источника не имеет права ослабить проверку подписи."""
    # SignatureDoesNotMatch - код ошибки хранилища, к подписи манифеста он
    # отношения не имеет. Проверяется то, что помощник не трогает закреплённый
    # ключ и не решает сам, доверять ли копии.
    assert "DENSTOCK_MANIFEST" not in SOURCE
    assert "verify_manifest" not in SOURCE
    assert "PublicKey" not in SOURCE
    launcher_json = json.dumps(LAUNCHER)
    assert "Assert-ReleaseIdentity" in launcher_json, "проверка версии релиза исчезла"
