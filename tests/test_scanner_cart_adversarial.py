"""Корзина сканера: враждебные сценарии, не покрытые основным набором.

Здесь проверяется не счастливый путь, а поведение под гонками, повторами и
частично аномальным состоянием: несколько лотов одной детали, конкуренция за
остаток, повторное проведение, повторная отмена и отмена мультипозиционной
продажи из любой её журнальной записи.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.cart import (
    KIND_REPAIR,
    KIND_SALE,
    add_scan,
    cart_rows,
    complete_cart,
    open_cart,
    set_row_quantity,
)
from apps.actions.models import WarehouseAction
from apps.actions.services import ActionError, cancel_warehouse_action
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot, sell_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale
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
        batch=batch,
        part_type=part,
        quantity=Decimal(str(qty)),
        unit_cost_currency=Decimal(unit_cost),
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
    loc1 = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка 2", code="S02-D01-C01", storage_allowed=True, is_active=True
    )
    bolt = PartType.objects.create(
        name="Болт",
        category=cat,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=bolt, value="700100", kind=PartNumber.Kind.OEM)
    return {
        "sup": sup,
        "admin": admin,
        "loc1": loc1,
        "loc2": loc2,
        "bolt": bolt,
        # Два лота ОДНОЙ детали в ОДНОЙ ячейке: проверяем FIFO-раскладку.
        "lot_a": _lot(bolt, loc1, 3, sup, admin, unit_cost="100"),
        "lot_b": _lot(bolt, loc1, 5, sup, admin, unit_cost="200"),
        "lot_far": _lot(bolt, loc2, 4, sup, admin),
    }


def _login(client, make_user, *, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


# --- Несколько лотов одной детали в одной ячейке ---------------------------------------


def test_one_visible_row_spans_several_lots_fifo(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("5"), by=data["admin"])

    rows = cart_rows(cart)
    assert len(rows) == 1  # пользователь видит одну позицию
    assert rows[0].quantity == Decimal("5")
    # Внутри документа сохраняется полотовая раскладка FIFO.
    lines = list(cart.lines.order_by("pk"))
    assert len(lines) == 2
    assert lines[0].stock_lot_id == data["lot_a"].pk
    assert lines[0].quantity == Decimal("3")
    assert lines[1].stock_lot_id == data["lot_b"].pk
    assert lines[1].quantity == Decimal("2")


def test_quantity_edit_resplits_lots_and_keeps_one_row(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("5"), by=data["admin"])
    set_row_quantity(cart, data["bolt"], data["loc1"], "2", by=data["admin"])

    rows = cart_rows(cart)
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("2")
    lines = list(cart.lines.all())
    assert len(lines) == 1
    assert lines[0].stock_lot_id == data["lot_a"].pk  # снова с самого раннего лота


def test_completing_multi_lot_row_consumes_each_lot(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("5"), by=data["admin"])
    complete_cart(cart, customer_comment="Иванов", by=data["admin"])

    data["lot_a"].refresh_from_db()
    data["lot_b"].refresh_from_db()
    assert data["lot_a"].quantity == Decimal("0")
    assert data["lot_b"].quantity == Decimal("3")


# --- Конкуренция за остаток ------------------------------------------------------------


def test_stock_sold_by_someone_else_blocks_completion(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("3"), by=data["admin"])
    movements_before = StockMovement.objects.count()

    # Пока корзина стояла, деталь ушла другим документом.
    sell_stock_lot(data["lot_a"], Decimal("3"), by=data["admin"], document_id=0)

    with pytest.raises(ActionError):
        complete_cart(cart, customer_comment="Иванов", by=data["admin"])

    cart.refresh_from_db()
    assert cart.status == Sale.Status.DRAFT  # документ не проведён
    data["lot_a"].refresh_from_db()
    assert data["lot_a"].quantity == Decimal("0")  # в минус не ушли
    assert StockMovement.objects.count() == movements_before + 1  # только чужая продажа


def test_repeated_complete_does_not_double_deduct(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    complete_cart(cart, customer_comment="Иванов", by=data["admin"])

    with pytest.raises(ActionError):
        complete_cart(cart, customer_comment="Иванов", by=data["admin"])

    data["lot_a"].refresh_from_db()
    assert data["lot_a"].quantity == Decimal("1")  # списано ровно один раз


def test_repeated_complete_of_repair_does_not_double_issue(data):
    cart = open_cart(KIND_REPAIR, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    complete_cart(cart, customer_comment="Петров", by=data["admin"])

    with pytest.raises(ActionError):
        complete_cart(cart, customer_comment="Петров", by=data["admin"])

    cart.refresh_from_db()
    assert cart.status == RepairOrder.Status.COMPLETED
    data["lot_a"].refresh_from_db()
    assert data["lot_a"].quantity == Decimal("1")


# --- Сторно мультипозиционной продажи --------------------------------------------------


def test_cancel_from_any_action_cancels_whole_document(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    add_scan(cart, data["bolt"], data["loc2"], quantity=Decimal("1"), by=data["admin"])
    actions = complete_cart(cart, customer_comment="Иванов", by=data["admin"])
    assert len(actions) == 2

    # Отмена из ВТОРОЙ записи журнала, а не из первой.
    cancel_warehouse_action(actions[1], by=data["admin"], reason="ошибка кассира")

    for action in actions:
        action.refresh_from_db()
        assert action.status == WarehouseAction.Status.CANCELLED
    cart.refresh_from_db()
    assert cart.status == Sale.Status.VOIDED


def test_repeated_cancel_is_rejected_and_returns_stock_once(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    actions = complete_cart(cart, customer_comment="Иванов", by=data["admin"])
    cancel_warehouse_action(actions[0], by=data["admin"], reason="ошибка")

    returned = sum(
        lot.quantity
        for lot in StockLot.objects.filter(
            part_type=data["bolt"], location=data["loc1"], status=StockLot.Status.AVAILABLE
        )
    )

    with pytest.raises(ActionError):
        cancel_warehouse_action(actions[0], by=data["admin"], reason="ещё раз")

    after = sum(
        lot.quantity
        for lot in StockLot.objects.filter(
            part_type=data["bolt"], location=data["loc1"], status=StockLot.Status.AVAILABLE
        )
    )
    assert after == returned  # второй возврат не произошёл


def test_cancel_after_sibling_action_already_cancelled(data):
    """Частично аномальное состояние: одна запись уже отменена вручную."""
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    add_scan(cart, data["bolt"], data["loc2"], quantity=Decimal("1"), by=data["admin"])
    actions = complete_cart(cart, customer_comment="Иванов", by=data["admin"])

    WarehouseAction.objects.filter(pk=actions[0].pk).update(status=WarehouseAction.Status.CANCELLED)
    # Документ ещё не сторнирован, поэтому отмена через вторую запись обязана
    # пройти и привести журнал в согласованное состояние.
    cancel_warehouse_action(actions[1], by=data["admin"], reason="доотмена")

    cart.refresh_from_db()
    assert cart.status == Sale.Status.VOIDED
    assert not WarehouseAction.objects.filter(
        sale_id=cart.pk, status=WarehouseAction.Status.ACTIVE
    ).exists()


def test_cancel_multi_location_returns_to_each_own_cell(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("3"), by=data["admin"])
    add_scan(cart, data["bolt"], data["loc2"], quantity=Decimal("4"), by=data["admin"])
    actions = complete_cart(cart, customer_comment="Иванов", by=data["admin"])
    cancel_warehouse_action(actions[0], by=data["admin"], reason="возврат")

    for location, expected in ((data["loc1"], Decimal("8")), (data["loc2"], Decimal("4"))):
        available = sum(
            lot.quantity
            for lot in StockLot.objects.filter(
                part_type=data["bolt"], location=location, status=StockLot.Status.AVAILABLE
            )
        )
        assert available == expected, location.code


# --- Двойная отправка формы через UI ----------------------------------------------------


def test_double_submit_of_complete_form_does_not_double_deduct(client, make_user, data):
    _login(client, make_user)
    client.post(
        reverse("actions_cart_add"),
        {
            "part_id": data["bolt"].pk,
            "location_id": data["loc1"].pk,
            "action_type": "sale",
            "quantity": "2",
            "q": "700100",
        },
    )
    from apps.customers.models import Customer

    payload = {
        "kind": KIND_SALE,
        "customer_id": Customer.objects.create(name="Иванов").pk,
        "q": "700100",
        "request_token": "double-submit-token",
    }
    first = client.post(reverse("actions_cart_complete"), payload, follow=True)
    assert first.status_code == 200
    second = client.post(reverse("actions_cart_complete"), payload, follow=True)
    assert second.status_code == 200

    assert Sale.objects.filter(status=Sale.Status.COMPLETED).count() == 1
    data["lot_a"].refresh_from_db()
    assert data["lot_a"].quantity == Decimal("1")
