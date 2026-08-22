"""Оживление уже существующей копии на Windows.

Правило в .gitattributes решает, каким файл приедет в следующий раз. Оно НЕ
переписывает то, что уже лежит на диске: Git считает такую копию актуальной и
просто её не трогает, а git status показывает чистое дерево. Измерено на
воспроизведении копии склада - после получения исправления сценарий оставался с
возвратом каретки, и WSL по-прежнему отвечал бы «invalid option name».

Поэтому нужен отдельный шаг. Здесь закреплено и то, почему обычные способы не
подходят, и то, что выбранный работает и ничего постороннего не задевает.

Репозиторий для проверки собирается здесь же, с нуля. Так проверка не зависит
от истории проекта и одинаково воспроизводит поведение Windows на любой машине:
core.autocrlf включается явно.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "operations"
REPAIR = OPS / "Repair-DenisStockShellLineEndings.ps1"

POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="нужен Windows PowerShell")

SCRIPT_BODY = b"#!/usr/bin/env bash\nset -euo pipefail\necho gotovo\n"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )


def carriage_returns(path: Path) -> int:
    return path.read_bytes().count(b"\r")


def line_feeds(path: Path) -> int:
    return path.read_bytes().count(b"\n")


@pytest.fixture
def stale_checkout(tmp_path: Path) -> Path:
    """Копия в том же состоянии, в каком её застаёт компьютер склада.

    Сценарий уже лежит на диске с возвратом каретки, правило про окончания
    строк уже получено, дерево при этом чистое.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    assert git(repo, "init", "--quiet").returncode == 0
    for key, value in (
        ("core.autocrlf", "true"),
        ("user.email", "proverka@example.invalid"),
        ("user.name", "Проверка"),
        ("commit.gpgsign", "false"),
    ):
        assert git(repo, "config", key, value).returncode == 0

    script = repo / "scripts" / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(SCRIPT_BODY)
    assert git(repo, "add", "scripts/run.sh").returncode == 0
    assert git(repo, "commit", "--quiet", "-m", "sluzhebnyy").returncode == 0

    # Получение файла заново - ровно то, что делает установка релиза.
    script.unlink()
    assert git(repo, "checkout", "--", "scripts/run.sh").returncode == 0
    assert carriage_returns(script) > 0, "не удалось воспроизвести Windows-копию"

    # Приезжает исправление с правилом.
    (repo / ".gitattributes").write_bytes(b"*.sh text eol=lf\n")
    assert git(repo, "add", ".gitattributes").returncode == 0
    assert git(repo, "commit", "--quiet", "-m", "pravilo").returncode == 0
    return repo


# --- Почему нужен отдельный шаг ---------------------------------------------------


def test_the_rule_alone_does_not_repair_an_existing_copy(stale_checkout: Path):
    """Главный факт: получить исправление недостаточно."""
    script = stale_checkout / "scripts" / "run.sh"
    assert carriage_returns(script) > 0, "копия внезапно исправилась сама"
    assert not git(stale_checkout, "status", "--porcelain").stdout.strip(), (
        "дерево должно выглядеть чистым: именно поэтому проблему не видно"
    )


def test_git_says_the_rule_is_in_force_while_the_file_is_still_wrong(stale_checkout: Path):
    """Расхождение видно только Git: правило есть, а на диске старое."""
    row = git(stale_checkout, "ls-files", "--eol", "--", "scripts/run.sh").stdout
    assert "attr/text eol=lf" in row
    assert "w/crlf" in row


def test_plain_checkout_of_the_path_changes_nothing(stale_checkout: Path):
    """Первое, что придёт в голову оператору, не работает."""
    script = stale_checkout / "scripts" / "run.sh"
    before = carriage_returns(script)
    assert git(stale_checkout, "checkout", "--", "scripts/run.sh").returncode == 0
    assert carriage_returns(script) == before


def test_restore_of_the_path_changes_nothing(stale_checkout: Path):
    script = stale_checkout / "scripts" / "run.sh"
    before = carriage_returns(script)
    assert git(stale_checkout, "restore", "scripts/run.sh").returncode == 0
    assert carriage_returns(script) == before


def test_deleting_the_file_first_is_what_actually_works(stale_checkout: Path):
    """Выбранный способ. Файл возвращается тут же и по новому правилу."""
    script = stale_checkout / "scripts" / "run.sh"
    script.unlink()
    assert git(stale_checkout, "checkout", "--", "scripts/run.sh").returncode == 0
    assert carriage_returns(script) == 0
    assert line_feeds(script) > 0


def test_touching_the_file_is_fragile_and_therefore_not_used(stale_checkout: Path):
    """Обновление времени срабатывает, но только пока никто не смотрел статус.

    Оператор почти наверняка заглянет в git status, а любая такая команда
    освежает кеш состояния, и получение снова становится пустым. Этот случай
    закреплён, чтобы способ не вернули как «более мягкий».
    """
    script = stale_checkout / "scripts" / "run.sh"
    script.touch()
    git(stale_checkout, "status", "--porcelain")
    assert git(stale_checkout, "checkout", "--", "scripts/run.sh").returncode == 0
    assert carriage_returns(script) > 0, "способ перестал быть хрупким: проверка устарела"


# --- Сам ремонтный сценарий -------------------------------------------------------


def test_the_repair_script_is_shipped():
    assert REPAIR.is_file(), "без него оператору нечего запускать"
    assert REPAIR.read_bytes().startswith(b"\xef\xbb\xbf"), "нет метки UTF-8"


def test_the_preflight_points_at_the_repair_script():
    preflight = (OPS / "Test-DenisStockEmergencyPreflight.ps1").read_text(encoding="utf-8-sig")
    assert "Repair-DenisStockShellLineEndings.ps1" in preflight


def test_the_installer_refuses_to_run_a_windows_script_inside_linux():
    installer = (
        OPS / "Install-DenisStock-EmergencyWorkstation.ps1"
    ).read_text(encoding="utf-8-sig")
    assert "Repair-DenisStockShellLineEndings.ps1" in installer
    # Заслон стоит до запуска, а не после: иначе оператор снова получит
    # невнятное сообщение от bash.
    guard = installer.index("ReadAllBytes($bootstrapPath)")
    launch = installer.index("bash $bootstrap")
    assert guard < launch


def run_repair(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    """Через -Command, а не -File: только так задаётся кодировка вывода."""
    command = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$OutputEncoding = [Text.Encoding]::UTF8; "
        f"& '{REPAIR}' -RepoRoot '{repo}' {' '.join(extra)}"
    )
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


@needs_powershell
def test_check_only_reports_the_problem_and_changes_nothing(stale_checkout: Path):
    script = stale_checkout / "scripts" / "run.sh"
    before = script.read_bytes()
    result = run_repair(stale_checkout, "-CheckOnly")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "scripts/run.sh" in result.stdout
    assert script.read_bytes() == before, "проверка не должна ничего менять"


@needs_powershell
def test_the_repair_makes_the_script_runnable_by_linux(stale_checkout: Path):
    script = stale_checkout / "scripts" / "run.sh"
    result = run_repair(stale_checkout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert carriage_returns(script) == 0
    assert line_feeds(script) > 0
    assert script.read_bytes() == SCRIPT_BODY, "содержимое изменилось, а не только формат"


@needs_powershell
def test_the_repair_keeps_the_working_state_of_the_station(stale_checkout: Path):
    """Идентификатор станции и настройки не отслеживаются и терять их нельзя."""
    runtime = stale_checkout / ".emergency"
    runtime.mkdir()
    (runtime / "workstation-id.txt").write_text("0f3b1d52-9c8e-4a77-b6f1-2d4c8e5a9b31")
    notes = stale_checkout / "ZAMETKI.txt"
    notes.write_text("заметки оператора", encoding="utf-8")

    assert run_repair(stale_checkout).returncode == 0
    assert (runtime / "workstation-id.txt").is_file()
    assert notes.read_text(encoding="utf-8") == "заметки оператора"


@needs_powershell
def test_the_repair_does_not_discard_a_local_edit_of_another_file(stale_checkout: Path):
    readme = stale_checkout / "README.md"
    readme.write_bytes(b"pravka operatora\n")
    git(stale_checkout, "add", "README.md")
    git(stale_checkout, "commit", "--quiet", "-m", "readme")
    readme.write_bytes(b"pravka operatora\nvtoraya stroka\n")

    assert run_repair(stale_checkout).returncode == 0
    assert b"vtoraya stroka" in readme.read_bytes(), "правку оператора стёрли"


@needs_powershell
def test_a_script_with_unsaved_edits_is_left_alone_and_reported(stale_checkout: Path):
    """Решение о судьбе правки принимает человек, а не сценарий.

    Правка вносится в том же виде, в каком файл лежит на Windows: с возвратом
    каретки. Иначе чинить было бы нечего и проверка ничего бы не значила.
    """
    script = stale_checkout / "scripts" / "run.sh"
    windows_style = SCRIPT_BODY.replace(b"\n", b"\r\n")
    script.write_bytes(windows_style.replace(b"gotovo", b"gotovo-pravlenno"))
    assert carriage_returns(script) > 0

    result = run_repair(stale_checkout)
    assert result.returncode == 1, result.stdout + result.stderr
    assert b"gotovo-pravlenno" in script.read_bytes(), "правку в сценарии стёрли"
    assert "несохранённые правки" in result.stdout


@needs_powershell
def test_a_healthy_copy_is_reported_as_healthy(stale_checkout: Path):
    assert run_repair(stale_checkout).returncode == 0
    second = run_repair(stale_checkout)
    assert second.returncode == 0
    assert "уже в порядке" in second.stdout, "повторный запуск должен быть спокойным"
