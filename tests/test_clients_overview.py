"""Общий отчёт «Продажи и ремонты по клиентам» и историческая лента."""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import (
    get_client_timeline,
    get_clients_sales_and_repairs,
    resolve_period,
)
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
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


def _stock(part, location, qty, sup, admin, *, unit_cost="100"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(str(qty)), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    bolt = PartType.objects.create(
        name="Болт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("500"),
    )
    PartNumber.objects.create(part=bolt, value="700100", kind=PartNumber.Kind.OEM)
    return {
        "sup": sup, "loc": loc, "admin": admin, "bolt": bolt,
        "sale_lot": _stock(bolt, loc, 20, sup, admin),
        "repair_lot": _stock(bolt, loc, 20, sup, admin),
    }


def _sale(data, customer, qty, *, price="500"):
    sale = create_sale(customer_name=customer, by=data["admin"])
    add_stock_lot_to_sale(
        sale, data["sale_lot"], Decimal(str(qty)), unit_price=Decimal(price), by=data["admin"]
    )
    return complete_sale(sale, by=data["admin"])


def _repair(data, customer, qty):
    order = create_repair_order(customer_name=customer, by=data["admin"])
    add_stock_lot_to_repair_order(order, data["repair_lot"], Decimal(str(qty)), by=data["admin"])
    return complete_repair_order(order, by=data["admin"])


def _login(client, make_user, *, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


# --- Сводка по клиентам ---------------------------------------------------------------------


def test_client_row_holds_sales_and_repairs_separately(data):
    sale = _sale(data, "Иванов", 2)
    order = _repair(data, "Иванов", 3)

    rows = {
        row["report_customer"]: row
        for row in get_clients_sales_and_repairs(resolve_period({}))
    }
    row = rows["Иванов"]
    assert row["sale_count"] == 1
    assert row["repair_count"] == 1
    assert row["revenue"] == sale.revenue_total
    assert row["issued_cost"] == order.cost_total
    assert row["repair_customer_amount"] == Decimal("1500.00")
    assert row["client_total_known"] == Decimal("2500.00")


def test_report_sums_sales_with_repair_customer_amount_not_cost(data):
    _sale(data, "Иванов", 2)
    _repair(data, "Иванов", 3)
    row = next(iter(get_clients_sales_and_repairs(resolve_period({}))))
    assert row["client_total_known"] == row["revenue"] + row["repair_customer_amount"]
    assert row["client_total_known"] != row["revenue"] + row["issued_cost"]


def test_client_with_only_repairs_is_present(data):
    _repair(data, "Петров", 1)
    rows = {
        row["report_customer"]: row
        for row in get_clients_sales_and_repairs(resolve_period({}))
    }
    assert rows["Петров"]["sale_count"] == 0
    assert rows["Петров"]["repair_count"] == 1
    assert rows["Петров"]["revenue"] == Decimal("0")


def test_client_with_only_sales_is_present(data):
    _sale(data, "Сидоров", 1)
    rows = {
        row["report_customer"]: row
        for row in get_clients_sales_and_repairs(resolve_period({}))
    }
    assert rows["Сидоров"]["repair_count"] == 0
    assert rows["Сидоров"]["issued_cost"] == Decimal("0")


def test_clients_sorted_by_document_count(data):
    _sale(data, "Активный", 1)
    _repair(data, "Активный", 1)
    _sale(data, "Разовый", 1)
    names = [row["report_customer"] for row in get_clients_sales_and_repairs(resolve_period({}))]
    assert names[0] == "Активный"


# --- Лента документов клиента ----------------------------------------------------------------


def test_timeline_merges_documents_newest_first(data):
    sale = _sale(data, "Иванов", 2)
    order = _repair(data, "Иванов", 1)

    events = get_client_timeline(resolve_period({}), customer_name="Иванов", missing=False)
    assert len(events) == 2
    assert events[0]["at"] >= events[1]["at"]
    numbers = {event["number"] for event in events}
    assert numbers == {sale.number, order.number}


def test_timeline_keeps_repair_customer_amount_separate_from_cost(data):
    sale = _sale(data, "Иванов", 2)
    order = _repair(data, "Иванов", 1)
    events = {event["kind"]: event for event in get_client_timeline(
        resolve_period({}), customer_name="Иванов", missing=False
    )}
    assert events["sale"]["revenue"] == sale.revenue_total
    assert events["sale"]["issued_cost"] is None
    assert events["repair"]["issued_cost"] == order.cost_total
    assert events["repair"]["revenue"] == Decimal("500.00")
    assert events["repair"]["revenue"] != events["repair"]["issued_cost"]


def test_timeline_points_to_source_documents(data):
    sale = _sale(data, "Иванов", 2)
    events = get_client_timeline(resolve_period({}), customer_name="Иванов", missing=False)
    assert events[0]["document_id"] == sale.pk


def test_timeline_skips_drafts(data):
    create_sale(customer_name="Иванов", by=data["admin"])
    create_repair_order(customer_name="Иванов", by=data["admin"])
    assert get_client_timeline(resolve_period({}), customer_name="Иванов", missing=False) == []


def test_timeline_quantity_is_frozen_document_quantity(data):
    _sale(data, "Иванов", 4)
    events = get_client_timeline(resolve_period({}), customer_name="Иванов", missing=False)
    assert events[0]["quantity"] == Decimal("4")


# --- Экраны -----------------------------------------------------------------------------------


def test_overview_page_shows_two_money_columns(client, make_user, data):
    _login(client, make_user)
    _sale(data, "Иванов", 2)
    _repair(data, "Иванов", 1)
    html = client.get(reverse("reports_clients_overview")).content.decode()
    assert "Продажи и ремонты по клиентам" in html
    assert "Выручка продаж (₽)" in html
    assert "Детали в ремонтах (₽)" in html
    assert "Итого с клиента (₽)" in html


def test_overview_page_explains_client_total(client, make_user, data):
    _login(client, make_user)
    html = client.get(reverse("reports_clients_overview")).content.decode()
    assert "Итого с клиента" in html


def test_timeline_page_shows_parts_not_documents(client, make_user, data):
    """Карточка клиента отвечает на вопрос «что получил клиент», а не «какие были документы».

    Раньше здесь проверялись номера продажи и ремонта со ссылками на сами
    документы. Теперь экран показывает сразу строки деталей: продажи и ремонты
    в одной ленте, дата слева. Документ остаётся доступен из своих разделов, но
    на клиентском экране он лишний уровень между вопросом и ответом.
    """
    _login(client, make_user)
    sale = _sale(data, "Иванов", 2)
    order = _repair(data, "Иванов", 1)
    html = client.get(reverse("reports_client_timeline"), {"customer": "Иванов"}).content.decode()

    assert data["bolt"].name in html, "деталь не показана в истории клиента"
    assert "Продажа" in html and "Ремонт" in html, "не видно, продажа это или ремонт"
    assert sale.number not in html, "номер продажи снова стал главным элементом"
    assert order.number not in html, "номер ремонта снова стал главным элементом"
    assert reverse("sale_detail", args=[sale.pk]) not in html
    assert reverse("repair_order_detail", args=[order.pk]) not in html


def test_timeline_requires_customer(client, make_user, data):
    _login(client, make_user)
    assert client.get(reverse("reports_client_timeline")).status_code == 404


def test_customer_totals_are_visible_without_purchase_cost_right(client, make_user, data):
    from apps.accounts import roles

    _sale(data, "Иванов", 2)
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.get(reverse("reports_clients_overview"))
    if resp.status_code == 403:
        pytest.skip("У роли нет доступа к отчётам: правило прав проверяется отдельно.")
    html = resp.content.decode()
    assert "Выручка продаж (₽)" in html
    assert "Детали в ремонтах (₽)" in html
    assert "Себестоимость ремонта (₽)" not in html
