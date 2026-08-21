"""У продукта должны быть собственные страницы 404 и 500.

Раньше настроен был только handler403, поэтому на неверный адрес и на сбой
пользователь получал служебную страницу Django: английский текст без объяснения
и без пути назад. При серверной ошибке нельзя обещать, что данные не изменились:
сбой может произойти уже после подтверждённой бизнес-транзакции.

Отдельно закрепляется то, что шаблон 500 остаётся автономным. Он рендерится с
ПУСТЫМ контекстом, а сама ошибка вполне может быть вызвана недоступностью базы,
поэтому наследовать базовый шаблон и обращаться к базе он не имеет права.
"""
import pytest
from django.template.loader import get_template, render_to_string


def test_missing_page_renders_the_product_404(client, db):
    response = client.get("/no-such-page-12345/")
    assert response.status_code == 404
    body = response.content.decode()
    assert "Страница не найдена" in body


def test_500_template_renders_with_an_empty_context():
    """Главное свойство: страница обязана собираться без контекста и без базы."""
    body = render_to_string("500.html", {})
    assert "Сбой на сервере" in body
    assert "Перед повторной операцией проверьте" in body
    assert "Данные склада не изменены" not in body
    assert "истории действий" in body


def test_500_template_does_not_extend_the_base_template():
    """Наследование вернуло бы зависимость от контекст-процессоров и базы."""
    source = get_template("500.html").template.source
    assert "{% extends" not in source
    assert "{% url" not in source


@pytest.mark.parametrize("name", ["404.html", "500.html"])
def test_error_templates_are_loadable(name):
    assert get_template(name) is not None


@pytest.mark.parametrize("name", ["403.html", "404.html", "500.html"])
def test_error_pages_start_with_a_doctype(name):
    """Текст выше <!DOCTYPE> переводит браузер в режим совместимости.

    Так и было: пояснение к шаблону 500 записали многострочным {# … #}, а такую
    запись Django не распознаёт и печатает как обычный текст. По исходнику это
    не видно, комментарий выглядит правильным, поэтому проверяется результат.
    """
    body = render_to_string(name, {})
    assert body.lstrip().startswith("<!DOCTYPE html>"), f"{name}: перед доктайпом есть текст"


@pytest.mark.parametrize("name", ["403.html", "404.html", "500.html"])
def test_error_pages_do_not_leak_template_syntax(name):
    body = render_to_string(name, {})
    assert "{#" not in body
    assert "{%" not in body
