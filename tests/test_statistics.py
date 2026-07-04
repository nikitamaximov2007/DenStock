"""Layer 27 — «Статистика»: read-only срез состояния склада.

Проверяем: доступ по can_view_finance; пустой склад не ломает страницу; KPI
считаются из landed cost реальных остатков; период влияет на залежавшихся и
активность; продажи попадают в «ходовые»; резервы с истёкшим сроком подсвечены;
страница ничего не пишет в БД и не содержит длинных тире.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockBalance, StockMovement
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.reports.statistics import get_statistics, resolve_stats_period
from apps.sales.models import Reservation
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    complete_sale,
    create_reservation,
    create_sale,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

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


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


def _finalized_line(sup, part, admin, *, qty, unit_cost="100", shipping="40"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal(shipping))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(qty), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def data(db, admin):
    """Склад с деньгами: 1 доступный экземпляр + лот 5 шт (landed cost > 0)."""
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    serial = PartType.objects.create(
        name="Насос-Стат", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("500"),
    )
    iline = _finalized_line(sup, serial, admin, qty="2")
    item = create_part_items(iline, 1, serial_number="SN-STAT-1")[0]
    receive_part_item(item, to_location=loc, by=admin)
    item.refresh_from_db()

    bulk = PartType.objects.create(
        name="Болт-Стат", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    bline = _finalized_line(sup, bulk, admin, qty="10")
    lot = create_stock_lot(bline, loc, Decimal("5"))
    receive_stock_lot(lot, by=admin)
    lot.refresh_from_db()

    return {
        "admin": admin, "serial": serial, "item": item,
        "bulk": bulk, "lot": lot, "loc": loc,
    }


def _login(client, make_user, *, role=None, superuser=False, name="u"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


# --- Доступ (гейт can_view_finance) -------------------------------------------


def test_requires_login(client, db):
    resp = client.get(reverse("statistics_dashboard"))
    assert resp.status_code == 302


def test_opens_for_admin(client, make_user):
    _login(client, make_user, superuser=True)
    resp = client.get(reverse("statistics_dashboard"))
    assert resp.status_code == 200
    assert "Статистика склада" in resp.content.decode()


@pytest.mark.parametrize("role", [roles.MANAGER, roles.VIEWER])
def test_opens_for_finance_roles(client, make_user, role):
    _login(client, make_user, role=role)
    assert client.get(reverse("statistics_dashboard")).status_code == 200


@pytest.mark.parametrize("role", [roles.SELLER, roles.STOREKEEPER])
def test_forbidden_without_finance(client, make_user, role):
    _login(client, make_user, role=role)
    assert client.get(reverse("statistics_dashboard")).status_code == 403


def test_nav_item_is_active_link(client, make_user):
    """Пункт меню больше не заглушка: у руководителя это ссылка на /statistics/."""
    _login(client, make_user, role=roles.MANAGER)
    html = client.get(reverse("dashboard")).content.decode()
    assert 'href="/statistics/"' in html
    client.logout()
    _login(client, make_user, role=roles.STOREKEEPER, name="sklad")
    assert 'href="/statistics/"' not in client.get(reverse("dashboard")).content.decode()


# --- Пустой склад ---------------------------------------------------------------


def test_empty_warehouse_does_not_break(client, make_user):
    _login(client, make_user, superuser=True)
    html = client.get(reverse("statistics_dashboard")).content.decode()
    assert "Оценённых остатков нет" in html
    assert "Активных резервов нет" in html


# --- KPI и деньги из landed cost -------------------------------------------------


def test_kpi_stock_cost_matches_landed_cost(data):
    period = resolve_stats_period({})
    stats = get_statistics(period)
    expected = data["item"].landed_cost_rub + (
        data["lot"].quantity * data["lot"].landed_unit_cost_rub
    )
    assert stats.kpi.stock_cost == expected
    assert stats.kpi.part_types_with_stock == 2
    assert stats.kpi.locations_with_stock == 1
    # Потенциальная выручка: только у serial задана рекомендованная цена (1 шт x 500).
    assert stats.kpi.potential_revenue == Decimal("500.00")
    assert stats.kpi.total_available == Decimal("6")


def test_value_blocks_grouped(data):
    stats = get_statistics(resolve_stats_period({}))
    assert [r.name for r in stats.value_by_category] == ["Двигатель"]
    names = {r.name for r in stats.top_parts_by_value}
    assert names == {"Насос-Стат", "Болт-Стат"}


def test_low_stock_uses_min_level(data):
    bulk = data["bulk"]
    bulk.min_stock_level = Decimal("10")
    bulk.save(update_fields=["min_stock_level"])
    stats = get_statistics(resolve_stats_period({}))
    assert stats.kpi.low_stock_count == 1
    assert stats.low_stock[0].name == "Болт-Стат"
    assert stats.low_stock[0].available == Decimal("5")


# --- Период: залежавшиеся и активность --------------------------------------------


def test_fresh_stock_is_not_stale(data):
    stats = get_statistics(resolve_stats_period({"period": "30"}))
    assert stats.stale == []
    assert stats.activity_total > 0  # приёмки попали в активность


def test_backdated_movements_become_stale_and_leave_activity(data):
    old = timezone.now() - timedelta(days=40)
    StockMovement.objects.all().update(created_at=old)
    stats30 = get_statistics(resolve_stats_period({"period": "30"}))
    assert {r.name for r in stats30.stale} == {"Насос-Стат", "Болт-Стат"}
    assert stats30.activity_total == 0
    # «Всё время» видит движения, а порог залежавшихся становится 90 дней.
    stats_all = get_statistics(resolve_stats_period({"period": "all"}))
    assert stats_all.activity_total > 0
    assert stats_all.stale == []


# --- Ходовые позиции (продажи за период) -------------------------------------------


def test_completed_sale_appears_in_movers(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_sale(
        sale, data["lot"], Decimal("2"), unit_price=Decimal("300"), by=data["admin"]
    )
    complete_sale(sale, by=data["admin"])
    stats = get_statistics(resolve_stats_period({"period": "7"}))
    assert len(stats.movers) == 1
    mover = stats.movers[0]
    assert mover.name == "Болт-Стат"
    assert mover.quantity == Decimal("2")
    assert mover.revenue == Decimal("600.00")
    # Продажа видна и в активности периода.
    labels = {r.label: r.count for r in stats.activity}
    assert labels["Продажи"] == 1


def test_no_sales_means_empty_movers(data):
    stats = get_statistics(resolve_stats_period({"period": "7"}))
    assert stats.movers == []


# --- Резервы, требующие внимания ----------------------------------------------------


def test_expired_active_reservation_flagged(data):
    r = create_reservation(customer_name="Пётр", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("1"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    Reservation.objects.filter(pk=r.pk).update(
        expires_at=timezone.now() - timedelta(days=1)
    )
    stats = get_statistics(resolve_stats_period({}))
    assert stats.kpi.active_reservations == 1
    row = stats.attention_reservations[0]
    assert row["reservation"].pk == r.pk
    assert row["expired"] is True


# --- Read-only и гигиена страницы ---------------------------------------------------


def test_statistics_page_is_readonly(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    moves = StockMovement.objects.count()
    balances = sorted(StockBalance.objects.values_list("id", "quantity_available"))
    resp = client.get(reverse("statistics_dashboard") + "?period=7")
    assert resp.status_code == 200
    assert StockMovement.objects.count() == moves
    assert sorted(StockBalance.objects.values_list("id", "quantity_available")) == balances


def test_page_has_no_em_dash(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("statistics_dashboard")).content.decode()
    assert "—" not in html


def test_invalid_period_falls_back(client, make_user):
    _login(client, make_user, superuser=True)
    resp = client.get(reverse("statistics_dashboard") + "?period=bogus")
    assert resp.status_code == 200
    assert "30 дней" in resp.content.decode()
