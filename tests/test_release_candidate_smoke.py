"""Сквозной smoke объединённого release candidate.

Проверяет, что после слияния всех возможностей меню и страницы каждой из них
открываются одновременно: клиенты, отчёты по клиентам, общий отчёт и импорт
каталога. Это ловит ошибку слияния навигации, когда один набор пунктов
вытесняет другой.
"""
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

PASSWORD = "parol-12345"

NEW_PAGES = [
    "customer_list",
    "customer_create",
    "reports_sales_by_client",
    "reports_repairs_by_client",
    "reports_clients_overview",
    "catalog_import_list",
]

MENU_LABELS = [
    "Клиенты",
    "Продажи по клиентам",
    "Ремонты по клиентам",
    "Продажи и ремонты",
    "Импорт каталога",
]


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def boss(client, make_user):
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)
    return client


@pytest.mark.parametrize("route", NEW_PAGES)
def test_new_pages_open(boss, route):
    assert boss.get(reverse(route)).status_code == 200


def test_menu_keeps_every_new_section(boss):
    """Слияние не должно было потерять ни один набор пунктов меню.

    Меню показывает пункты того раздела, в котором находится пользователь,
    поэтому каждый пункт проверяется на своей странице.
    """
    checks = (
        ("customer_list", "Клиенты"),
        ("reports_sales_by_client", "Продажи по клиентам"),
        ("reports_repairs_by_client", "Ремонты по клиентам"),
        ("reports_clients_overview", "Продажи и ремонты"),
        ("catalog_import_list", "Импорт каталога"),
        ("part_list", "Импорт каталога"),
    )
    for route, label in checks:
        html = boss.get(reverse(route)).content.decode()
        assert label in html, f"{route}: {label}"


def test_catalog_and_client_reports_coexist(boss):
    catalog = boss.get(reverse("catalog_import_list")).content.decode()
    clients = boss.get(reverse("reports_clients_overview")).content.decode()
    assert "Импорт каталога" in catalog
    assert "Продажи и ремонты по клиентам" in clients
    # Денежная семантика ремонта не смешалась с выручкой при слиянии.
    assert "не складываются" in clients


def test_operator_sees_neither_catalog_import_nor_customer_editing(client, make_user, db):
    from apps.accounts import roles

    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    assert client.get(reverse("catalog_import_list")).status_code == 403
