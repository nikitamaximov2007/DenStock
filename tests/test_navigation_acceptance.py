"""Каждая ежедневная функция обязана находиться в меню, а не только по адресу.

Сегодня уже был реальный случай: импорт каталога BRP существовал, но найти его
было нечем. Этот тест закрывает весь класс: для каждой роли он открывает
настоящую страницу, забирает построенное меню из контекста и проверяет, что
нужные разделы в нём есть.

Проверяется доступность через меню, а не наличие маршрута. Функция, до которой
можно добраться только зная адрес, для сотрудника не существует.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles

PASSWORD = "parol-12345"

# Что сотрудник обязан находить сам, без подсказки разработчика.
#
# Просмотр всего каталога («Все детали», BRP, Polaris) сюда намеренно не входит:
# ежедневный путь к детали это «Поиск» в верхнем меню, а список каталога нужен
# редко. Его недоступность из меню зафиксирована в отчёте как отдельное
# наблюдение, а не как ежедневный блокер.
DAILY_FUNCTIONS = {
    roles.STOREKEEPER: [
        "balance_list",
        "warehouse_index",
        "receipt_list",
        "scanner_move",
        "movement_list",
        "actions_scan",
        "repair_order_list",
        "return_list",
    ],
    roles.SELLER: [
        "actions_scan",
        "repair_order_list",
        "customer_list",
    ],
    roles.MANAGER: [
        "balance_list",
        "receipt_list",
        "repair_order_list",
        "customer_list",
        "reports_dashboard",
        "catalog_import_list",
        "price_settings",
    ],
    roles.ADMIN: [
        "balance_list",
        "receipt_list",
        "repair_order_list",
        "customer_list",
        "reports_dashboard",
        "catalog_import_list",
        "price_settings",
        "user_list",
    ],
}


@pytest.fixture
def make_user(db, django_user_model):
    def _make(role):
        user = django_user_model.objects.create_user(username=f"user-{role}", password=PASSWORD)
        user.groups.add(Group.objects.get(name=role))
        return user

    return _make


NAV_KEYS = ("nav_items", "nav_groups", "section_tabs", "section_subtabs")


def _urls_on_page(response) -> set[str]:
    urls: set[str] = set()

    def _collect(value):
        if isinstance(value, dict):
            if isinstance(value.get("url"), str):
                urls.add(value["url"].split("?")[0])
            for item in value.values():
                _collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _collect(item)

    for key in NAV_KEYS:
        try:
            _collect(response.context[key])
        except KeyError:
            continue
    return urls


def _reachable_by_mouse(client) -> tuple[set[str], str]:
    """Обойти меню так, как это делает человек: главная, раздел, вкладки раздела.

    Вкладки строятся под текущий раздел, поэтому одной главной страницы мало:
    нужно зайти в каждый раздел меню и забрать его собственные вкладки.
    """
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200, "главная страница недоступна"

    reachable = _urls_on_page(response)
    # Плитки главной страницы это тоже путь мышью.
    body = response.content.decode()
    visited: set[str] = set()
    frontier = {url for url in reachable if url.startswith("/")}
    for url in sorted(frontier):
        if url in visited:
            continue
        visited.add(url)
        section = client.get(url)
        if section.status_code == 200 and section.context is not None:
            reachable |= _urls_on_page(section)
    return reachable, body


@pytest.mark.parametrize("role", sorted(DAILY_FUNCTIONS))
def test_every_daily_function_is_reachable_from_the_menu(client, make_user, role):
    make_user(role)
    client.login(username=f"user-{role}", password=PASSWORD)
    urls, body = _reachable_by_mouse(client)

    missing = []
    for name in DAILY_FUNCTIONS[role]:
        target = reverse(name)
        if target not in urls and f'href="{target}"' not in body:
            missing.append(f"{name} ({target})")

    assert not missing, (
        f"роль «{role}» не может дойти мышью до: {', '.join(missing)}. "
        "Функция, доступная только по прямому адресу, для сотрудника не существует"
    )


@pytest.mark.parametrize("role", sorted(DAILY_FUNCTIONS))
def test_menu_never_offers_a_link_that_answers_with_denied(client, make_user, role):
    """Показанный пункт меню обязан открываться, а не приводить к отказу.

    Обратное это худший вид тупика: сотрудник видит кнопку, нажимает и упирается
    в «нет доступа», не понимая, что сделал не так.
    """
    make_user(role)
    client.login(username=f"user-{role}", password=PASSWORD)
    urls, _ = _reachable_by_mouse(client)

    denied = []
    for url in sorted(urls):
        if not url.startswith("/"):
            continue
        response = client.get(url)
        if response.status_code in (403, 404, 500):
            denied.append(f"{url} -> {response.status_code}")

    assert not denied, f"меню роли «{role}» ведёт в отказ: {denied}"
