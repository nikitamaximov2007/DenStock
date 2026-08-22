"""Комментарии в шаблонах не должны попадать на экран.

Django распознаёт запись ``{# … #}`` только в пределах одной строки. Стоит
перенести такой комментарий на вторую строку - и он перестаёт быть
комментарием: движок печатает его читателю как обычный текст.

По исходнику это не видно. Комментарий выглядит правильным, подсветка в
редакторе показывает его серым, и заметить можно только на живой странице.
Найдено дважды: на странице ошибки 500, где пояснение оказалось над
``<!DOCTYPE>`` и переводило браузер в режим совместимости, и на форме быстрого
добавления детали, где пояснение встало между полями и кнопкой.

Поэтому проверяется весь каталог шаблонов сразу, а не отдельные страницы.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted(ROOT.glob("templates/**/*.html"))


def unterminated_comment_lines(text: str) -> list[tuple[int, str]]:
    """Строки, где ``{#`` открыт и не закрыт до конца этой же строки."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if "{#" not in line:
            continue
        tail = line.split("{#", 1)[1]
        if "#}" not in tail:
            found.append((number, line.strip()))
    return found


def test_there_are_templates_to_check():
    assert len(TEMPLATES) > 50, "шаблоны не найдены: проверка была бы пустой"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_template_opens_a_comment_it_never_closes(template: Path):
    """Многострочный комментарий надо писать через ``{% comment %}``."""
    text = open(template, encoding="utf-8").read()
    broken = unterminated_comment_lines(text)
    assert not broken, (
        f"{template.relative_to(ROOT)}: комментарий не закрыт на своей строке "
        f"{broken} - Django напечатает его на странице. "
        "Для нескольких строк используйте {% comment %} … {% endcomment %}."
    )


def test_the_check_actually_catches_the_mistake():
    """Проверка проверки: иначе она молчала бы и на настоящем дефекте."""
    leaking = "{# первая строка\n   вторая строка #}\n<p>текст</p>\n"
    assert unterminated_comment_lines(leaking) == [(1, "{# первая строка")]

    correct = "{# одна строка #}\n{% comment %}\n  много строк\n{% endcomment %}\n"
    assert unterminated_comment_lines(correct) == []
