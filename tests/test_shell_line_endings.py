"""Сценарии, которые исполняет Linux, обязаны приезжать с окончаниями LF.

На Windows у Git обычно включено core.autocrlf=true. Без явного правила он
переводит LF в CRLF при получении файлов из репозитория: в самом репозитории
всё правильно, а на диске оказывается CRLF. Linux этого не прощает.

Измерено на компьютере склада: bash получал "set -euo pipefail" с невидимым
возвратом каретки и отвечал "set: pipefail: invalid option name". Установка
аварийной станции на этом останавливалась, и по исходнику причину было не
увидеть: файл в репозитории выглядел безупречно.

Поэтому проверяется не текст файла, а то, что об этом файле говорит сам Git:
какой атрибут к нему применён и что лежит в индексе. Именно эта пара и решает,
что получит машина при следующем получении файлов.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CR = b"\r"


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


def tracked_shell_scripts() -> list[str]:
    return [line for line in git("ls-files", "*.sh").split("\n") if line.strip()]


SHELL_SCRIPTS = tracked_shell_scripts()

# Файлы не с расширением .sh, которые тоже читает Linux.
OTHER_LINUX_FILES = ("docker/Dockerfile",)


def parse_eol_row(row: str) -> dict:
    """Разобрать строку git ls-files --eol вида "i/lf w/lf attr/text eol=lf путь"."""
    fields, _, path = row.partition("\t")
    parts = fields.split()
    row_data = {"path": path.strip(), "index": "", "worktree": "", "attr": ""}
    for part in parts:
        if part.startswith("i/"):
            row_data["index"] = part[2:]
        elif part.startswith("w/"):
            row_data["worktree"] = part[2:]
    # Атрибут берётся целиком, а не по словам: он может состоять из нескольких,
    # например "text eol=lf".
    if "attr/" in fields:
        row_data["attr"] = fields.split("attr/", 1)[1].strip()
    return row_data


def eol_rows(paths) -> dict:
    output = git("ls-files", "--eol", "--", *paths)
    rows = {}
    for line in output.split("\n"):
        if not line.strip():
            continue
        parsed = parse_eol_row(line)
        rows[parsed["path"]] = parsed
    return rows


def test_the_repository_actually_has_shell_scripts():
    assert SHELL_SCRIPTS, "сценариев .sh не найдено: проверка была бы пустой"


def test_the_wsl_bootstrap_is_still_tracked():
    """Именно он ломался на компьютере склада."""
    assert "scripts/operations/provision-wsl-docker.sh" in SHELL_SCRIPTS


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_git_promises_lf_for_every_shell_script(script):
    """Атрибут решает, что получит машина при следующем получении файлов."""
    row = eol_rows([script])[script]
    assert "eol=lf" in row["attr"], (
        f"{script}: Git не обещает LF (атрибут «{row['attr']}»). "
        "На Windows такой файл приедет с CRLF и Linux его не примет."
    )


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_the_stored_content_is_lf(script):
    """В репозитории окончания строк тоже должны быть LF, а не только на диске."""
    row = eol_rows([script])[script]
    assert row["index"] == "lf", f"{script}: в индексе окончания «{row['index']}»"


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_the_checked_out_file_has_no_carriage_return(script):
    """Проверка того, что реально лежит на диске этой машины."""
    raw = (ROOT / script).read_bytes()
    assert CR not in raw, (
        f"{script}: на диске есть возврат каретки. "
        "Удалите файл и получите его заново: git checkout -- " + script
    )


@pytest.mark.parametrize("path", OTHER_LINUX_FILES)
def test_other_linux_files_are_pinned_to_lf(path):
    row = eol_rows([path])[path]
    assert "eol=lf" in row["attr"], f"{path}: нет обещания LF"


def test_the_attributes_file_exists_and_explains_itself():
    attributes = ROOT / ".gitattributes"
    assert attributes.is_file(), "без .gitattributes правило не действует вовсе"
    text = attributes.read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in text
    assert "autocrlf" in text, "правило без объяснения снимут при первой же уборке"


def test_powershell_scripts_keep_their_own_contract():
    """Правило про окончания строк не должно задеть метку UTF-8 у .ps1.

    Метка и окончания строк - разные вещи, но правку легко расширить на весь
    репозиторий, поэтому связь закреплена явно.
    """
    scripts = [line for line in git("ls-files", "*.ps1").split("\n") if line.strip()]
    assert scripts, "сценариев .ps1 не найдено"
    rows = eol_rows(scripts)
    for script in scripts:
        assert "eol=lf" not in rows[script]["attr"], (
            f"{script}: сценарий PowerShell попал под правило для Linux"
        )
        raw = (ROOT / script).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), (
            f"{script}: пропала метка UTF-8, Windows PowerShell сломает кириллицу"
        )
