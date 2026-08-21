"""Стоимость деталей, выданных в ремонт, в отчётах по клиенту.

Для продаж сумма показывалась, для ремонтов не показывалось ничего, и
сотрудник не мог узнать, во сколько складу обошлась выдача.

Главное правило, которое здесь закреплено: у ремонта нет выручки. Система
хранит себестоимость выданных деталей, замороженную при проведении заказа, и
складывать её с суммой продажи нельзя. Поэтому величины живут в разных
колонках, и у каждой строки заполнена ровно одна из них.

Второе правило: для прошлого ремонта нельзя брать сегодняшнюю цену из
каталога. Источник только исторический.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
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
from apps.reports.services import get_client_part_history, resolve_period
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
    return make_user("boss", is_superuser=True)


def _lot(part, location, qty, supplier, admin, *, unit_cost):
    """Партия со своей закупочной ценой: так задаётся историческая себестоимость."""
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(str(qty)), unit_cost_currency=Decimal(str(unit_cost)),
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
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S07-D01-C01", storage_allowed=True, is_active=True
    )
    parts, lots = {}, {}
    # Закупочные цены намеренно разные: так видно, что берётся своя у каждой.
    for key, name, number, cost in (
        ("belt", "Ремень", "800100", "300"),
        # 155 намеренно: при количестве 2 итог не совпадёт с ремнём за 300.
        ("filter", "Фильтр", "800200", "155"),
        ("plug", "Свеча", "800300", "90"),
    ):
        part = PartType.objects.create(
            name=name, category=category, unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal("999"),
        )
        PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM)
        parts[key] = part
        lots[key] = _lot(part, location, 100, supplier, admin, unit_cost=cost)
    return {"admin": admin, "parts": parts, "lots": lots, "loc": location, "sup": supplier}


def _repair(data, *, customer, items):
    order = create_repair_order(customer=customer, customer_name="", by=data["admin"])
    for key, qty in items:
        add_stock_lot_to_repair_order(order, data["lots"][key], Decimal(str(qty)), by=data["admin"])
    return complete_repair_order(order, by=data["admin"])


def _sale(data, *, customer, items, price="500"):
    sale = create_sale(customer=customer, customer_name="", by=data["admin"])
    for key, qty in items:
        add_stock_lot_to_sale(
            sale, data["lots"][key], Decimal(str(qty)), unit_price=Decimal(price), by=data["admin"]
        )
    return complete_sale(sale, by=data["admin"])


def _login(client, user):
    client.login(username=user.username, password=PASSWORD)


# --- Источник величины -------------------------------------------------------------


def test_the_repair_cost_comes_from_the_frozen_line(data):
    """Себестоимость замораживается при проведении и живёт на строке заказа."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1),))

    line = order.lines.get()
    assert line.unit_cost_rub > 0, "себестоимость единицы не заморожена"
    assert line.total_cost_rub == line.unit_cost_rub * line.quantity

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    repair_rows = [row for row in rows if row["kind"] == "repair"]
    assert len(repair_rows) == 1
    assert repair_rows[0]["cost"] == line.total_cost_rub


def test_a_later_catalog_price_change_does_not_move_a_past_repair(data):
    """Для прошлого ремонта сегодняшняя цена каталога значения не имеет."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 2),))
    before = order.lines.get().total_cost_rub

    part = data["parts"]["belt"]
    part.recommended_price = Decimal("99999")
    part.save(update_fields=["recommended_price"])

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["cost"] == before
    assert rows[0]["cost"] != Decimal("99999")


def test_quantity_is_already_included_in_the_line_total(data):
    """Показывается итог строки, а не цена единицы: делить ничего не нужно."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 3),))
    line = order.lines.get()

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["cost"] == line.unit_cost_rub * Decimal("3")


def test_each_part_of_a_repair_keeps_its_own_cost(data):
    """У деталей разная закупочная цена, и смешивать их нельзя."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1), ("filter", 2), ("plug", 1)))

    by_part = {line.part_type.name: line.total_cost_rub for line in order.lines.all()}
    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    from_report = {row["part_name"]: row["cost"] for row in rows if row["kind"] == "repair"}
    assert from_report == by_part
    assert len(set(by_part.values())) == 3, "проверка бессмысленна: себестоимости совпали"


# --- Разделение выручки и себестоимости ------------------------------------------------


def test_a_sale_row_never_carries_a_cost_and_a_repair_never_carries_revenue(data):
    """Их нельзя сложить, потому что они никогда не стоят в одном поле."""
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("belt", 1),))
    _repair(data, customer=customer, items=(("filter", 1),))

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    for row in rows:
        if row["kind"] == "sale":
            assert row["amount"] is not None
            assert row["cost"] is None, "у продажи появилась себестоимость"
        else:
            assert row["cost"] is not None
            assert row["amount"] is None, "себестоимость выдана за выручку"


def test_the_sale_amount_is_unchanged(data):
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("belt", 2),), price="500")

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    sale_rows = [row for row in rows if row["kind"] == "sale"]
    assert sale_rows[0]["amount"] == sale.lines.get().total_price == Decimal("1000")


# --- Оба экрана ------------------------------------------------------------------------


def test_the_combined_report_shows_the_two_values_in_separate_columns(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("belt", 1),))
    _repair(data, customer=customer, items=(("filter", 1),))

    body = client.get(
        reverse("reports_client_timeline"), {"customer_id": customer.pk}
    ).content.decode()
    assert "Сумма (₽)" in body
    assert "Себестоимость (₽)" in body
    assert "складывать их между собой нельзя" in body


def test_the_repairs_report_shows_the_cost(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 2),))
    expected = order.lines.get().total_cost_rub

    body = client.get(
        reverse("reports_repairs_by_client_detail"), {"customer_id": customer.pk}
    ).content.decode()
    assert "Себестоимость (₽)" in body
    assert "выручка" not in body.lower() or "не выручка" in body.lower()
    digits = f"{int(expected)}"
    assert digits in body.replace("&nbsp;", "").replace(" ", ""), "себестоимость не показана"


def test_the_repairs_report_never_calls_it_revenue(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer, items=(("belt", 1),))

    body = client.get(
        reverse("reports_repairs_by_client_detail"), {"customer_id": customer.pk}
    ).content.decode()
    assert "Выручка" not in body, "себестоимость названа выручкой"


# --- Права и период ----------------------------------------------------------------------


def test_a_role_without_cost_rights_sees_no_cost(client, data, make_user):
    """Себестоимость закрыта тем же правом, что и остальные закупочные суммы."""
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer, items=(("belt", 1),))
    keeper = make_user("kladovshik", role=roles.STOREKEEPER)
    _login(client, keeper)

    response = client.get(
        reverse("reports_repairs_by_client_detail"), {"customer_id": customer.pk}
    )
    assert response.status_code == 200
    assert response.context["show_money"] is False
    assert "Себестоимость (₽)" not in response.content.decode()


def test_a_repair_outside_the_period_is_not_shown(data):
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1),))
    RepairOrder.objects.filter(pk=order.pk).update(
        completed_at=timezone.now() - timezone.timedelta(days=400)
    )

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert not [row for row in rows if row["kind"] == "repair"]


def test_two_repairs_are_not_counted_twice(data):
    customer = Customer.objects.create(name="Иванов")
    first = _repair(data, customer=customer, items=(("belt", 1),))
    second = _repair(data, customer=customer, items=(("belt", 1),))

    rows = [
        row for row in get_client_part_history(resolve_period({}), customer_id=customer.pk)
        if row["kind"] == "repair"
    ]
    assert len(rows) == 2
    total = sum(row["cost"] for row in rows)
    expected = first.lines.get().total_cost_rub + second.lines.get().total_cost_rub
    assert total == expected
