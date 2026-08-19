"""Долгая операция обязана выглядеть как идущая, а не как зависшая.

Полное применение каталога BRP занимает до минуты. Всё это время страница молчит,
поэтому сотрудник решает, что не нажалось, и нажимает снова. Данные при этом не
страдают: повторное применение блокируется на уровне базы и отклоняется понятной
ошибкой. Но человек видит красное сообщение об ошибке после успешной операции и
не понимает, что произошло.

В проекте уже есть готовый механизм: `data-idempotent-form` в app_shell.js
блокирует кнопку на время отправки и подменяет надпись. Здесь закрепляется, что
он применён к долгим формам и что механизм не исчез.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
APP_SHELL = ROOT / "static" / "js" / "app_shell.js"

# Формы, за которыми сотрудник реально ждёт больше секунды.
SLOW_FORMS = [
    ("catalog_import/import_detail.html", "catalog_import_apply"),
    ("catalog_import/import_detail.html", "catalog_import_recheck"),
]


def _form_block(source: str, action_name: str) -> str:
    """Вырезать разметку формы, отправляющей на указанный маршрут."""
    match = re.search(
        r"<form[^>]*?" + re.escape(action_name) + r".*?</form>", source, re.S
    )
    assert match, f"форма с действием {action_name} не найдена"
    return match.group(0)


@pytest.mark.parametrize(("template", "action_name"), SLOW_FORMS)
def test_slow_form_disables_itself_while_running(template, action_name):
    source = (TEMPLATES / template).read_text(encoding="utf-8")
    block = _form_block(source, action_name)
    assert "data-idempotent-form" in block, (
        f"{action_name}: долгая форма без защиты от повторного нажатия"
    )


@pytest.mark.parametrize(("template", "action_name"), SLOW_FORMS)
def test_slow_form_tells_the_user_it_is_working(template, action_name):
    source = (TEMPLATES / template).read_text(encoding="utf-8")
    block = _form_block(source, action_name)
    assert "data-progress-label" in block, (
        f"{action_name}: кнопка не сообщает, что операция идёт"
    )


def test_catalog_apply_warns_about_the_duration_before_the_click():
    """Предупреждение до нажатия важнее индикатора после него."""
    source = (TEMPLATES / "catalog_import" / "import_detail.html").read_text(
        encoding="utf-8"
    )
    block = _form_block(source, "catalog_import_apply")
    assert "минуты" in block, "пользователя не предупредили, что применение долгое"


def test_the_shared_guard_still_exists():
    """Механизм общий: если он исчезнет, защита пропадёт сразу везде."""
    source = APP_SHELL.read_text(encoding="utf-8")
    assert "data-idempotent-form" in source
    assert "button.disabled = true" in source
    assert "progressLabel" in source


def test_every_form_that_declares_the_guard_has_a_submit_button():
    """Защита без кнопки отправки ничего не даёт."""
    for path in TEMPLATES.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        if "data-idempotent-form" not in source:
            continue
        for block in re.findall(r"<form[^>]*data-idempotent-form.*?</form>", source, re.S):
            assert 'type="submit"' in block, (
                f"{path.name}: форма с защитой не имеет кнопки отправки"
            )
