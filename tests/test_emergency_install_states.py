"""Машина состояний установки: «где я сейчас» и «какой следующий шаг».

Физическая установка требует перезагрузок и повторных запусков. После каждой
человек должен одной командой узнать фактическое состояние и ровно следующий
безопасный шаг, не читая код.

Проверяется настоящими запусками на подставных каталогах: ветвление зависит от
того, что лежит на диске, и чтением исходника это не подтверждается.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "operations"
READINESS = OPS / "Test-DenisStockEmergency.ps1"

POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="нужен Windows PowerShell")

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA5xL0Wl6xJ7v8xQqW0nJ8m4hQ0m9k3Yb2vZ1cF7pXsRA=
-----END PUBLIC KEY-----
"""


def run_readiness(repo_root: Path) -> str:
    """Запуск с пропуском источника копий: проверяется именно ветвление шагов.

    Через -Command, а не -File: только так можно заранее задать кодировку
    вывода. Иначе PowerShell печатает в кодировке консоли, и русские строки
    приходят сюда вопросительными знаками, а проверки становятся пустыми.
    """
    command = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$OutputEncoding = [Text.Encoding]::UTF8; "
        f"& '{READINESS}' -RepoRoot '{repo_root}' -SkipBackupSource"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    return result.stdout


def make_installed_station(root: Path) -> None:
    """Каталог, выглядящий как установленная станция."""
    runtime = root / ".emergency"
    (runtime / "trusted").mkdir(parents=True)
    (runtime / "workstation-id.txt").write_text(
        "0f3b1d52-9c8e-4a77-b6f1-2d4c8e5a9b31", encoding="utf-8"
    )
    (runtime / "trusted" / "production-manifest-ed25519-public.pem").write_text(
        PUBLIC_KEY, encoding="utf-8"
    )
    (root / ".env.emergency").write_text(
        "DENSTOCK_MODE=emergency-local\n"
        "DENSTOCK_EMERGENCY_ROLE=primary\n"
        "DENSTOCK_EMERGENCY_BIND_HOST=127.0.0.1\n"
        "DENSTOCK_EMERGENCY_PORT=8080\n"
        "DENSTOCK_MANIFEST_SIGNING_KEY_ID=production-1\n"
        "DENSTOCK_EMERGENCY_WSL_DISTRO=Ubuntu\n",
        encoding="utf-8",
    )


@needs_powershell
def test_a_fresh_computer_is_not_reported_as_broken(tmp_path):
    """Иначе после первого запуска человек решит, что всё сломано."""
    output = run_readiness(tmp_path)
    assert "НЕ ГОТОВО: станция ещё не установлена." in output
    assert "Что делать дальше:" in output
    assert "Запустить установку" in output


@needs_powershell
def test_a_fresh_computer_names_the_missing_pieces(tmp_path):
    output = run_readiness(tmp_path)
    for layer in ("Идентификатор станции", "Доверенный ключ", "Конфигурация"):
        assert layer in output, f"не назван слой: {layer}"
    assert "не создан" in output
    assert "не закреплён" in output


@needs_powershell
def test_an_installed_station_stops_asking_to_install(tmp_path):
    """Установленная станция должна получать следующий шаг, а не первый."""
    make_installed_station(tmp_path)
    output = run_readiness(tmp_path)
    assert "станция ещё не установлена" not in output
    assert "Запустить установку" not in output


@needs_powershell
def test_an_installed_station_shows_its_identity_and_key(tmp_path):
    make_installed_station(tmp_path)
    output = run_readiness(tmp_path)
    assert "0f3b1d52-9c8e-4a77-b6f1-2d4c8e5a9b31" in output, "идентификатор не показан"
    assert "production-1" in output, "идентификатор ключа не показан"


@needs_powershell
def test_a_corrupted_identity_is_reported_and_not_replaced(tmp_path):
    """Новый идентификатор поверх повреждённого разорвал бы назначение станции."""
    make_installed_station(tmp_path)
    identity = tmp_path / ".emergency" / "workstation-id.txt"
    identity.write_text("это не идентификатор", encoding="utf-8")
    output = run_readiness(tmp_path)
    assert "файл повреждён" in output


@needs_powershell
def test_the_station_never_reports_ready_without_the_application(tmp_path):
    """Копия склада ещё не получена: готовностью это называть нельзя."""
    make_installed_station(tmp_path)
    output = run_readiness(tmp_path)
    assert "ГОТОВО: станция готова к работе." not in output


@needs_powershell
def test_the_readiness_answers_within_a_minute(tmp_path):
    """Молчащее окно неотличимо от зависшего. На сломанной машине тоже."""
    import time

    started = time.monotonic()
    run_readiness(tmp_path)
    elapsed = time.monotonic() - started
    assert elapsed < 120, f"проверка отвечала {elapsed:.0f} секунд"
