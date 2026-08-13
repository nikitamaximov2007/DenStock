"""Отчёты по клиентам после появления справочника.

Главная гарантия: отчёт не выдаёт совпадение текста за одного человека.

* документы, связанные с карточкой, группируются по её идентификатору, поэтому
  переименование карточки не дробит строку отчёта;
* документы без карточки остаются отдельными историческими строками и явно
  помечаются «без карточки»;
* два разных клиента с одинаковым именем не сливаются в одну строку;
* денежная семантика не изменилась: выручка продаж и себестоимость выданного в
  ремонт остаются разными величинами и не складываются.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import (
    get_client_timeline,
    get_clients_sales_and_repairs,
    get_customer_part_sales,
    get_repairs_by_customer,
    get_sales_by_customer,
    resolve_period,
)
from apps.sales.models import Sale
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


def _lot(part, location, qty, sup, admin, *, unit_cost="100"):
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
        "sale_lot": _lot(bolt, loc, 50, sup, admin),
        "repair_lot": _lot(bolt, loc, 50, sup, admin),
    }


def _sale(data, *, customer=None, name="", qty=1):
    sale = create_sale(customer=customer, customer_name=name, by=data["admin"])
    add_stock_lot_to_sale(
        sale, data["sale_lot"], Decimal(str(qty)), unit_price=Decimal("500"), by=data["admin"]
    )
    return complete_sale(sale, by=data["admin"])


def _repair(data, *, customer=None, name="", qty=1):
    order = create_repair_order(customer=customer, customer_name=name, by=data["admin"])
    add_stock_lot_to_repair_order(order, data["repair_lot"], Decimal(str(qty)), by=data["admin"])
    return complete_repair_order(order, by=data["admin"])


def _login(client, make_user, *, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


def _rows_by_name(rows):
    return {row["display_name"]: row for row in rows}


# --- Группировка по карточке ------------------------------------------------------------


def test_linked_sales_group_by_card_not_by_text(data):
    customer = Customer.objects.create(name="Иванов", phone="+79121234567")
    _sale(data, customer=customer, qty=2)
    # Карточку переименовали между продажами: снимки будут разными.
    customer.name = "Иванов Иван"
    customer.save()
    _sale(data, customer=customer, qty=3)

    rows = get_sales_by_customer(resolve_period({}))
    assert len(rows) == 1  # одна строка, а не две по разным снимкам
    row = rows[0]
    assert row["linked"] is True
    assert row["customer_id"] == customer.pk
    assert row["display_name"] == "Иванов Иван"  # текущее имя карточки
    assert row["quantity"] == Decimal("5")


def test_namesakes_with_cards_stay_separate(data):
    first = Customer.objects.create(name="Иван Иванов")
    second = Customer.objects.create(name="Иван Иванов")
    _sale(data, customer=first, qty=1)
    _sale(data, customer=second, qty=2)

    rows = get_sales_by_customer(resolve_period({}))
    assert len(rows) == 2
    assert {row["customer_id"] for row in rows} == {first.pk, second.pk}


def test_legacy_documents_are_marked_and_not_merged_with_card(data):
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, qty=1)
    _sale(data, name="Иванов", qty=4)  # без карточки, но то же имя

    rows = get_sales_by_customer(resolve_period({}))
    assert len(rows) == 2
    linked = [row for row in rows if row["linked"]]
    legacy = [row for row in rows if not row["linked"]]
    assert len(linked) == 1 and len(legacy) == 1
    assert linked[0]["quantity"] == Decimal("1")
    assert legacy[0]["quantity"] == Decimal("4")
    assert legacy[0]["display_name"] == "Иванов"


def test_repairs_group_by_card_too(data):
    customer = Customer.objects.create(name="Петров")
    _repair(data, customer=customer, qty=2)
    _repair(data, name="Петров", qty=5)

    rows = get_repairs_by_customer(resolve_period({}))
    assert len(rows) == 2
    by_link = {row["linked"]: row for row in rows}
    assert by_link[True]["quantity"] == Decimal("2")
    assert by_link[False]["quantity"] == Decimal("5")


def test_document_without_name_is_reported_as_missing(data):
    order = RepairOrder.objects.create(customer_name="", created_by=data["admin"])
    add_stock_lot_to_repair_order(order, data["repair_lot"], Decimal("1"), by=data["admin"])
    complete_repair_order(order, by=data["admin"])

    rows = get_repairs_by_customer(resolve_period({}))
    assert rows[0]["display_name"] == "Без клиента"
    assert rows[0]["missing_name"] is True


# --- Детализация и лента -----------------------------------------------------------------


def test_card_detail_collects_documents_across_renames(data):
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, qty=2)
    customer.name = "Иванов Иван"
    customer.save()
    _sale(data, customer=customer, qty=1)

    rows = list(get_customer_part_sales(resolve_period({}), customer_id=customer.pk))
    assert len(rows) == 1
    assert rows[0]["quantity"] == Decimal("3")


def test_card_detail_excludes_legacy_namesake(data):
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, qty=2)
    _sale(data, name="Иванов", qty=7)

    linked = list(get_customer_part_sales(resolve_period({}), customer_id=customer.pk))
    legacy = list(
        get_customer_part_sales(resolve_period({}), customer_name="Иванов", missing=False)
    )
    assert linked[0]["quantity"] == Decimal("2")
    assert legacy[0]["quantity"] == Decimal("7")


def test_timeline_of_card_mixes_sales_and_repairs(data):
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, qty=1)
    order = _repair(data, customer=customer, qty=1)

    events = get_client_timeline(resolve_period({}), customer_id=customer.pk)
    assert {event["number"] for event in events} == {sale.number, order.number}
    assert events[0]["at"] >= events[1]["at"]


def test_timeline_of_legacy_name_does_not_include_card_documents(data):
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, qty=1)
    legacy_sale = _sale(data, name="Иванов", qty=1)

    events = get_client_timeline(resolve_period({}), customer_name="Иванов", missing=False)
    assert [event["number"] for event in events] == [legacy_sale.number]


def test_combined_report_keeps_card_and_legacy_apart(data):
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, qty=1)
    _repair(data, customer=customer, qty=1)
    _sale(data, name="Иванов", qty=1)

    rows = get_clients_sales_and_repairs(resolve_period({}))
    assert len(rows) == 2
    linked = next(row for row in rows if row["linked"])
    legacy = next(row for row in rows if not row["linked"])
    assert linked["sale_count"] == 1 and linked["repair_count"] == 1
    assert legacy["sale_count"] == 1 and legacy["repair_count"] == 0


def test_combined_report_never_sums_money(data):
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, qty=1)
    _repair(data, customer=customer, qty=1)
    row = get_clients_sales_and_repairs(resolve_period({}))[0]
    assert row["revenue"] > 0
    assert row["issued_cost"] > 0
    for forbidden in ("total", "total_money", "combined_total", "turnover"):
        assert forbidden not in row


# --- Экраны -------------------------------------------------------------------------------


def test_report_pages_link_card_and_mark_legacy(client, make_user, data):
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, qty=1)
    _sale(data, name="Сидоров", qty=1)

    html = client.get(reverse("reports_sales_by_client")).content.decode()
    assert f"customer_id={customer.pk}" in html
    assert "без карточки" in html


def test_card_detail_page_opens_by_customer_id(client, make_user, data):
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, qty=1)

    resp = client.get(
        reverse("reports_sales_by_client_detail"), {"customer_id": customer.pk}
    )
    assert resp.status_code == 200
    assert "Иванов" in resp.content.decode()


def test_unknown_customer_id_is_404(client, make_user, data):
    _login(client, make_user)
    assert (
        client.get(reverse("reports_sales_by_client_detail"), {"customer_id": 999999}).status_code
        == 404
    )


def test_legacy_detail_page_still_opens_by_name(client, make_user, data):
    _login(client, make_user)
    _sale(data, name="Сидоров", qty=1)
    resp = client.get(reverse("reports_sales_by_client_detail"), {"customer": "Сидоров"})
    assert resp.status_code == 200


def test_timeline_page_opens_for_card(client, make_user, data):
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, qty=1)
    resp = client.get(reverse("reports_client_timeline"), {"customer_id": customer.pk})
    assert resp.status_code == 200
    assert sale.number in resp.content.decode()


def test_period_boundaries_are_respected(data):
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, qty=1)
    Sale.objects.filter(pk=sale.pk).update(
        sold_at=timezone.now() - timezone.timedelta(days=400)
    )
    assert get_sales_by_customer(resolve_period({})) == []


# --- Производительность ----------------------------------------------------------------------


def test_reports_query_count_does_not_grow_with_documents(
    client, make_user, data, django_assert_max_num_queries
):
    """Отчёты по клиентам не должны давать N+1 по документам."""
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов")
    for _ in range(3):
        _sale(data, customer=customer, qty=1)
        _repair(data, customer=customer, qty=1)

    urls = (
        reverse("reports_sales_by_client"),
        reverse("reports_repairs_by_client"),
        reverse("reports_clients_overview"),
    )
    for url in urls:
        client.get(url)  # прогрев
    baseline = {}
    for url in urls:
        with django_assert_max_num_queries(20) as captured:
            client.get(url)
        baseline[url] = len(captured)

    for _ in range(6):
        _sale(data, customer=customer, qty=1)
        _repair(data, customer=customer, qty=1)

    for url in urls:
        with django_assert_max_num_queries(baseline[url]) as captured:
            client.get(url)
        assert len(captured) <= baseline[url], url


def test_client_timeline_query_count_is_bounded(
    client, make_user, data, django_assert_max_num_queries
):
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов")
    for _ in range(4):
        _sale(data, customer=customer, qty=1)
        _repair(data, customer=customer, qty=1)

    url = reverse("reports_client_timeline")
    client.get(url, {"customer_id": customer.pk})
    with django_assert_max_num_queries(15):
        client.get(url, {"customer_id": customer.pk})
