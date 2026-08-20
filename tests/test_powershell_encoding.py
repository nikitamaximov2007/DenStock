"""Сценарии PowerShell должны читаться так же, как их читает Windows.

Windows PowerShell определяет кодировку файла .ps1 по метке в начале. Без метки
файл читается в системной кодировке, а не как UTF-8, и весь русский текст в нём
превращается в мусор. Сценарий с русскими сообщениями тогда не запускается
вовсе: он падает на разборе, ещё не начав работу.

Проверить это чтением файла как UTF-8 нельзя: так он выглядит исправным. Именно
поэтому дефект дожил до подготовки установки, хотя проверка синтаксиса в
прошлых прогонах на него указывала.

Установщик, ярлыки на рабочем столе и задание в планировщике запускают сценарии
именно через Windows PowerShell, поэтому метка обязательна.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"
SCRIPTS = sorted(ROOT.glob("scripts/**/*.ps1"))


def test_there_are_powershell_scripts_to_check():
    assert SCRIPTS, "сценарии не найдены: проверка была бы пустой"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_starts_with_a_utf8_marker(script):
    raw = script.read_bytes()
    assert raw.startswith(BOM), (
        f"{script.name} без метки UTF-8: на русской Windows он не запустится"
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_body_after_the_marker_is_valid_utf8(script):
    raw = script.read_bytes()
    body = raw[len(BOM):] if raw.startswith(BOM) else raw
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        pytest.fail(f"{script.name} не является UTF-8: {exc}")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_script_carries_private_key_material(script):
    """Ищется именно вставленный ключ, а не упоминание.

    Сборщик диагностики держит эти же слова как образец поиска утечки, поэтому
    проверять надо полную рамку PEM с дефисами: в образце поиска её нет.
    """
    text = script.read_text(encoding="utf-8-sig")
    for forbidden in (
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
    ):
        assert forbidden not in text, f"{script.name} содержит приватный ключ"
