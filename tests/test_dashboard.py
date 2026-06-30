"""v1.1.2 — полезный dashboard (P0 из аудита).

Дашборд — информационный read-only экран: читает существующие сервисы отчётов и
простые счётчики, ничего не пишет. Финансы — только под правом; быстрые действия —
по capability. Складскую физику не трогает.
"""
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockBalance, StockMovement
from apps.sales.models import Sale

PASSWORD = "parol-12345"


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


def _login(client, make_user, role):
    make_user("u", role=role)
    client.login(username="u", password=PASSWORD)


def _html(client):
    return client.get(reverse("dashboard")).content.decode()


# --- Доступность / отсутствие мёртвых плиток ---------------------------------


def test_dashboard_opens_for_authenticated(client, make_user):
    _login(client, make_user, roles.MANAGER)
    resp = client.get(reverse("dashboard"))
    assert resp.status_code == 200
    assert "Быстрые действия" in resp.content.decode()


def test_no_dead_placeholder_tiles(client, make_user):
    _login(client, make_user, roles.MANAGER)
    html = _html(client)
    # Старые «мёртвые» плитки-плейсхолдеры убраны.
    assert "Прибыль за период" not in html
    assert "Продажи сегодня" not in html
    assert "Поиск детали" in html


# --- Empty states вместо «—» -------------------------------------------------


def test_empty_state_on_empty_base(client, make_user):
    _login(client, make_user, roles.MANAGER)
    assert "Деталей пока нет" in _html(client)


def test_kpi_reflects_catalog(client, make_user):
    _login(client, make_user, roles.MANAGER)
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    PartType.objects.create(
        name="Деталь-А", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    html = _html(client)
    assert "Деталей пока нет" not in html  # каталог не пуст
    assert "Видов деталей" in html  # KPI-плитка на месте


# --- Быстрые действия по правам ----------------------------------------------


def test_quick_actions_storekeeper(client, make_user):
    _login(client, make_user, roles.STOREKEEPER)
    html = _html(client)
    assert "Приёмка сканером" in html  # есть manage_inventory
    assert "Новая продажа" not in html  # нет manage_sales


def test_quick_actions_seller(client, make_user):
    _login(client, make_user, roles.SELLER)
    html = _html(client)
    assert "Новая продажа" in html  # есть manage_sales
    assert "Приёмка сканером" not in html  # нет manage_inventory


def test_seller_without_reports_sees_actions_only(client, make_user):
    _login(client, make_user, roles.SELLER)
    html = _html(client)
    assert "Быстрые действия" in html
    assert "Видов деталей" not in html  # KPI скрыты без VIEW_REPORTS


# --- Финансы только под правом -----------------------------------------------


def test_financial_kpi_hidden_without_cost_right(client, make_user):
    # Кладовщик: VIEW_REPORTS есть, VIEW_PURCHASE_COST — нет.
    _login(client, make_user, roles.STOREKEEPER)
    html = _html(client)
    assert "Выручка" not in html
    assert "Прибыль" not in html


def test_financial_kpi_shown_with_cost_right(client, make_user):
    _login(client, make_user, roles.MANAGER)
    assert "Выручка за месяц" in _html(client)


# --- Read-only относительно склада -------------------------------------------


def test_dashboard_is_read_only(client, make_user):
    _login(client, make_user, roles.MANAGER)
    mv_before = StockMovement.objects.count()
    bal_before = sorted(StockBalance.objects.values_list("id", "quantity_available"))
    sales_before = Sale.objects.count()
    client.get(reverse("dashboard"))
    client.get(reverse("dashboard"))
    assert StockMovement.objects.count() == mv_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_available")) == bal_before
    assert Sale.objects.count() == sales_before
