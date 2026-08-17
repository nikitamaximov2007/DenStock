"""Поля сканера обязаны быть защищены от мобильной автозамены.

Сканер работает как клавиатура, но те же экраны открывают и с телефона, где
номер детали набирают руками. Мобильная автозамена и автокапитализация меняют
буквенно-цифровую строку, и поиск не находит существующую деталь.

Проверка идёт по разметке, а не по одному экрану: новый экран со сканером
обязан получить ту же защиту.
"""
import pathlib

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "templates"
REQUIRED = ('autocapitalize="off"', 'autocorrect="off"', 'spellcheck="false"')


def _scanner_templates():
    return sorted(
        path
        for path in TEMPLATES.rglob("*.html")
        if "data-scan-input" in path.read_text(encoding="utf-8")
    )


def test_scanner_templates_are_discovered():
    assert _scanner_templates(), "не найдено ни одного поля сканера"


@pytest.mark.parametrize("path", _scanner_templates(), ids=lambda p: p.name)
def test_scanner_input_is_protected_from_mobile_autocorrection(path):
    text = path.read_text(encoding="utf-8")
    for attribute in REQUIRED:
        assert attribute in text, (
            f"{path.name}: поле сканера без {attribute}, "
            "мобильная автозамена исказит номер детали"
        )


@pytest.mark.parametrize("path", _scanner_templates(), ids=lambda p: p.name)
def test_scanner_input_does_not_autocomplete(path):
    assert 'autocomplete="off"' in path.read_text(encoding="utf-8")
