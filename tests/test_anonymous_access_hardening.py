"""Складские эндпоинты обязаны отвечать входом, а не аварией.

Проверки прав в этих представлениях читали `request.user.can_...` напрямую.
У `AnonymousUser` такого атрибута нет, поэтому неаутентифицированный запрос
получал AttributeError, то есть 500. Доступ при этом не выдавался и склад не
менялся, но поведение было неверным: оператор с истёкшей сессией, открывший
закладку, видел аварийную страницу вместо формы входа, а мониторинг получал
поток 500, скрывающий настоящие инциденты.

Здесь закрепляется правильный ответ и одновременно то, что права для
аутентифицированных пользователей не ослабли.
"""
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles

PASSWORD = "parol-12345"

# Эндпоинты, меняющие склад, партии или структуру хранения.
GUARDED = [
    ("item_create", [1]),
    ("item_bulk_create", [1]),
    ("item_status_change", [1]),
    ("item_receive", [1]),
    ("item_move", [1]),
    ("lot_create", [1]),
    ("lot_create_remaining", [1]),
    ("lot_edit", [1]),
    ("lot_status_change", [1]),
    ("lot_receive", [1]),
    ("lot_move", [1]),
    ("lot_adjust", [1]),
    ("batch_status_change", [1]),
    ("batch_cost_preview", [1]),
    ("batch_cost_finalize", [1]),
    ("batch_line_delete", [1]),
    ("location_toggle", [1]),
]


@pytest.fixture
def seller(db, django_user_model):
    """Роль без прав на склад, партии и структуру хранения."""
    user = django_user_model.objects.create_user(username="seller", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.SELLER))
    return user


@pytest.mark.parametrize(("name", "args"), GUARDED)
@pytest.mark.parametrize("method", ["get", "post"])
def test_anonymous_is_sent_to_login_not_to_an_error(client, db, name, args, method):
    response = getattr(client, method)(reverse(name, args=args))
    assert response.status_code in (301, 302), (
        f"{name} ответил {response.status_code} вместо перенаправления на вход"
    )
    assert "/login" in response["Location"]


@pytest.mark.parametrize(("name", "args"), GUARDED)
def test_authenticated_without_capability_still_gets_403(client, seller, name, args):
    """Контроль: перенаправление добавлено, но проверка прав не ослабла."""
    client.login(username="seller", password=PASSWORD)
    response = client.post(reverse(name, args=args), {})
    assert response.status_code == 403, (
        f"{name} ответил {response.status_code}, а роль без прав обязана получать 403"
    )
