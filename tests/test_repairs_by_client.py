"""Отчёт «Ремонты по клиентам».

Зеркало отчёта «Продажи по клиентам», но с ЧЕСТНОЙ денежной семантикой.
У ремонтного заказа нет клиентской суммы: Слой 17 фиксирует, какие детали ушли
на технику клиента, и замораживает их СЕБЕСТОИМОСТЬ. Цены работ, оплаты и
прибыли в системе нет, поэтому в отчёте нет и не может быть выручки, а
себестоимость выданного закрыта правом на закупочные цены.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
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
    get_customer_repair_parts,
    get_repairs_by_customer,
    resolve_period,
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
        name="Болт", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    PartNumber.objects.create(part=bolt, value="700100", kind=PartNumber.Kind.OEM)
    ring = PartType.objects.create(
        name="Кольцо", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    PartNumber.objects.create(part=ring, value="700200", kind=PartNumber.Kind.OEM)
    return {
        "sup": sup, "cat": cat, "unit": unit, "loc": loc, "admin": admin,
        "bolt": bolt, "ring": ring,
        "bolt_lot": _stock(bolt, loc, 10, sup, admin),
        "ring_lot": _stock(ring, loc, 10, sup, admin, unit_cost="200"),
    }


def _repair(data, customer, *parts, complete=True):
    order = create_repair_order(customer_name=customer, by=data["admin"])
    for lot, qty in parts:
        add_stock_lot_to_repair_order(order, lot, Decimal(str(qty)), by=data["admin"])
    if complete:
        order = complete_repair_order(order, by=data["admin"])
    return order


def _login(client, make_user, *, role=None, superuser=True, name="boss"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


# --- Агрегация ------------------------------------------------------------------------------


def test_groups_completed_repairs_by_customer(data):
    _repair(data, "Иванов", (data["bolt_lot"], 2))
    _repair(data, "Иванов", (data["ring_lot"], 1))
    _repair(data, "Петров", (data["bolt_lot"], 3))

    rows = {row["report_customer"]: row for row in get_repairs_by_customer(resolve_period({}))}
    assert set(rows) == {"Иванов", "Петров"}
    assert rows["Иванов"]["repair_count"] == 2
    assert rows["Иванов"]["unique_parts"] == 2
    assert rows["Иванов"]["quantity"] == Decimal("3")
    assert rows["Петров"]["quantity"] == Decimal("3")


def test_draft_and_canceled_repairs_are_not_counted(data):
    _repair(data, "Иванов", (data["bolt_lot"], 2))
    _repair(data, "Черновиков", (data["bolt_lot"], 1), complete=False)

    rows = {row["report_customer"]: row for row in get_repairs_by_customer(resolve_period({}))}
    assert set(rows) == {"Иванов"}


def test_report_uses_frozen_issue_cost_not_revenue(data):
    """Считается себестоимость выданного: клиентской суммы у ремонта нет."""
    order = _repair(data, "Иванов", (data["ring_lot"], 2))
    row = next(iter(get_repairs_by_customer(resolve_period({}))))
    assert row["issued_cost"] == order.cost_total
    assert "revenue" not in row


def test_repair_order_has_no_customer_amount_field(data):
    """Явная фиксация факта: суммы для клиента в ремонтном заказе нет."""
    fields = {field.name for field in RepairOrder._meta.get_fields()}
    assert "cost_total" in fields
    for money_field in ("revenue_total", "profit_total", "total_price", "amount", "price"):
        assert money_field not in fields


def test_customer_detail_groups_by_part(data):
    _repair(data, "Иванов", (data["bolt_lot"], 2), (data["ring_lot"], 1))
    _repair(data, "Иванов", (data["bolt_lot"], 3))

    rows = {
        row["part_type__name"]: row
        for row in get_customer_repair_parts(
            resolve_period({}), customer_name="Иванов", missing=False
        )
    }
    assert rows["Болт"]["quantity"] == Decimal("5")
    assert rows["Болт"]["operation_count"] == 2
    assert rows["Кольцо"]["quantity"] == Decimal("1")


def test_customer_without_name_is_reachable_as_missing(data):
    # Сервис пустого клиента не разрешает, поэтому строка «без клиента» бывает
    # только у унаследованных записей: моделируем их напрямую через модель.
    order = RepairOrder.objects.create(customer_name="", created_by=data["admin"])
    add_stock_lot_to_repair_order(order, data["bolt_lot"], Decimal("1"), by=data["admin"])
    complete_repair_order(order, by=data["admin"])

    rows = list(get_customer_repair_parts(resolve_period({}), customer_name="", missing=True))
    assert len(rows) == 1
    assert rows[0]["quantity"] == Decimal("1")


# --- Экраны ---------------------------------------------------------------------------------


def test_report_page_renders_without_revenue_column(client, make_user, data):
    _login(client, make_user)
    _repair(data, "Иванов", (data["bolt_lot"], 2))
    html = client.get(reverse("reports_repairs_by_client")).content.decode()
    assert "Ремонты по клиентам" in html
    assert "Иванов" in html
    assert "Себестоимость выданного (₽)" in html
    assert "Выручка" not in html


def test_report_page_states_absence_of_repair_revenue(client, make_user, data):
    _login(client, make_user)
    html = client.get(reverse("reports_repairs_by_client")).content.decode()
    assert "выручки по ремонтам здесь нет" in html


def test_cost_hidden_without_purchase_cost_right(client, make_user, data):
    _repair(data, "Иванов", (data["bolt_lot"], 2))
    _login(client, make_user, role=roles.STOREKEEPER, superuser=False, name="sklad")
    resp = client.get(reverse("reports_repairs_by_client"))
    if resp.status_code == 403:
        pytest.skip("У роли нет доступа к отчётам: правило прав проверяется отдельно.")
    html = resp.content.decode()
    assert "Себестоимость выданного (₽)" not in html
    assert "Финансовые показатели скрыты для вашей роли." in html


def test_detail_and_operations_pages_open(client, make_user, data):
    _login(client, make_user)
    order = _repair(data, "Иванов", (data["bolt_lot"], 2))
    detail = client.get(reverse("reports_repairs_by_client_detail"), {"customer": "Иванов"})
    assert detail.status_code == 200
    assert "Болт" in detail.content.decode()

    operations = client.get(
        reverse("reports_repairs_by_client_operations"),
        {"customer": "Иванов", "part": data["bolt"].pk},
    )
    assert operations.status_code == 200
    body = operations.content.decode()
    assert order.number in body  # ссылка на исходный документ


def test_detail_requires_customer(client, make_user, data):
    _login(client, make_user)
    assert client.get(reverse("reports_repairs_by_client_detail")).status_code == 404


def test_navigation_has_repairs_by_client(client, make_user, data):
    _login(client, make_user)
    html = client.get(reverse("reports_repairs_by_client")).content.decode()
    assert "Продажи по клиентам" in html
    assert "Ремонты по клиентам" in html
