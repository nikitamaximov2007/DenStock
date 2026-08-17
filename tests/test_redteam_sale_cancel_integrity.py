"""Red-team: целостность сторно мультипозиционной продажи.

Проверяется самый опасный класс ошибки: возврат товара «не туда». Продажа
собирается максимально неудобной - несколько деталей, несколько ячеек,
несколько лотов у одной детали в одной ячейке - и отменяется из РАЗНЫХ записей
журнала. Результат обязан быть одинаковым независимо от того, из какой записи
инициирована отмена.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group

from apps.actions.cart import KIND_SALE, add_scan, complete_cart, open_cart
from apps.actions.models import WarehouseAction
from apps.actions.services import ActionError, cancel_warehouse_action
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        return django_user_model.objects.create_user(username=username, password=PASSWORD)

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
    loc1 = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка 2", code="S02-D01-C01", storage_allowed=True, is_active=True
    )
    bolt = PartType.objects.create(
        name="Болт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=bolt, value="700100", kind=PartNumber.Kind.OEM)
    ring = PartType.objects.create(
        name="Кольцо", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("250"),
    )
    PartNumber.objects.create(part=ring, value="700200", kind=PartNumber.Kind.OEM)
    return {
        "sup": sup, "admin": admin, "loc1": loc1, "loc2": loc2, "bolt": bolt, "ring": ring,
        # Две партии одной детали в одной ячейке: продажа разложится по двум лотам.
        "bolt_a": _lot(bolt, loc1, 3, sup, admin, unit_cost="100"),
        "bolt_b": _lot(bolt, loc1, 5, sup, admin, unit_cost="200"),
        "bolt_far": _lot(bolt, loc2, 4, sup, admin),
        "ring_lot": _lot(ring, loc2, 6, sup, admin),
    }


def _available(part, location):
    return sum(
        lot.quantity
        for lot in StockLot.objects.filter(
            part_type=part, location=location, status=StockLot.Status.AVAILABLE
        )
    )


def _snapshot(data):
    return {
        ("bolt", "loc1"): _available(data["bolt"], data["loc1"]),
        ("bolt", "loc2"): _available(data["bolt"], data["loc2"]),
        ("ring", "loc2"): _available(data["ring"], data["loc2"]),
    }


def _hard_sale(data):
    """Продажа из трёх позиций: две детали, две ячейки, два лота у одной."""
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("5"), by=data["admin"])
    add_scan(cart, data["bolt"], data["loc2"], quantity=Decimal("2"), by=data["admin"])
    add_scan(cart, data["ring"], data["loc2"], quantity=Decimal("3"), by=data["admin"])
    return cart, complete_cart(cart, customer_comment="Иванов", by=data["admin"])


@pytest.mark.parametrize("index", [0, 1, 2])
def test_cancel_from_any_action_restores_the_same_state(data, index):
    """Отмена из любой записи журнала даёт одинаковый результат."""
    before = _snapshot(data)
    cart, actions = _hard_sale(data)
    assert len(actions) == 3

    cancel_warehouse_action(actions[index], by=data["admin"], reason="проверка")

    assert _snapshot(data) == before, f"отмена из записи {index} вернула товар не туда"
    cart.refresh_from_db()
    assert cart.status == Sale.Status.VOIDED
    for action in actions:
        action.refresh_from_db()
        assert action.status == WarehouseAction.Status.CANCELLED


def test_cancel_restores_each_lot_not_just_each_cell(data):
    """Возврат уважает партии: себестоимость лотов не перемешивается."""
    lots_before = {
        lot.pk: lot.quantity for lot in StockLot.objects.filter(status=StockLot.Status.AVAILABLE)
    }
    _, actions = _hard_sale(data)
    cancel_warehouse_action(actions[2], by=data["admin"], reason="проверка")

    restored = {}
    for lot in StockLot.objects.filter(status=StockLot.Status.AVAILABLE):
        key = (lot.batch_line_id, lot.location_id, lot.landed_unit_cost_rub)
        restored[key] = restored.get(key, Decimal("0")) + lot.quantity
    original = {}
    for pk, quantity in lots_before.items():
        lot = StockLot.objects.get(pk=pk)
        key = (lot.batch_line_id, lot.location_id, lot.landed_unit_cost_rub)
        original[key] = original.get(key, Decimal("0")) + quantity
    assert restored == original


def test_repeated_cancel_never_adds_stock_twice(data):
    before = _snapshot(data)
    _, actions = _hard_sale(data)
    cancel_warehouse_action(actions[0], by=data["admin"], reason="первый раз")
    after_first = _snapshot(data)
    assert after_first == before

    for action in actions:
        with pytest.raises(ActionError):
            cancel_warehouse_action(action, by=data["admin"], reason="ещё раз")
    assert _snapshot(data) == before


def test_cancel_writes_only_return_movements(data):
    _, actions = _hard_sale(data)
    movements_before = StockMovement.objects.count()
    cancel_warehouse_action(actions[1], by=data["admin"], reason="проверка")
    added = StockMovement.objects.count() - movements_before
    # По одному компенсирующему движению на строку продажи, не больше.
    assert added == Sale.objects.get(pk=actions[1].sale_id).lines.count()


def test_cancel_keeps_document_totals_frozen(data):
    """Сторно не переписывает историческую выручку документа."""
    cart, actions = _hard_sale(data)
    revenue = Sale.objects.get(pk=cart.pk).revenue_total
    cancel_warehouse_action(actions[0], by=data["admin"], reason="проверка")
    assert Sale.objects.get(pk=cart.pk).revenue_total == revenue
