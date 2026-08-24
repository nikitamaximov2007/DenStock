from decimal import Decimal

import pytest

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    calculate_repair_customer_amount,
    complete_repair_order,
    create_repair_order,
    repair_customer_amounts,
)
from apps.returns.models import StockReturnLine
from apps.returns.services import (
    add_repair_line_return,
    cancel_return,
    complete_return,
    create_return,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.fixture
def repair_data(db, django_user_model):
    user = django_user_model.objects.create_superuser("repair-price", "x@example.test", "pass")
    supplier = Supplier.objects.create(name="Поставщик")
    category = Category.objects.create(name="Ремонт")
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    location = StorageLocation.objects.create(name="Ремонт", code="R-01", storage_allowed=True)

    def stock(name, price, quantity="10", cost="100"):
        part = PartType.objects.create(
            name=name,
            category=category,
            unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal(price) if price is not None else None,
        )
        batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch,
            part_type=part,
            quantity=Decimal(quantity),
            unit_cost_currency=Decimal(cost),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, user)
        line.refresh_from_db()
        lot = create_stock_lot(line, location, Decimal(quantity))
        receive_stock_lot(lot, by=user)
        return part, lot

    return user, location, stock


def _completed(user, lot, quantity, *, price=None):
    order = create_repair_order(customer_name="Клиент", by=user)
    add_stock_lot_to_repair_order(
        order, lot, Decimal(quantity), customer_unit_price_rub=price, by=user
    )
    complete_repair_order(order, by=user)
    return order


def test_customer_price_is_frozen_and_separate_from_cost(repair_data):
    user, _location, stock = repair_data
    part, lot = stock("Насос", "5000", cost="2500")
    order = _completed(user, lot, "1")
    line = order.lines.get()
    part.recommended_price = Decimal("9000")
    part.save(update_fields=["recommended_price"])
    assert line.customer_unit_price_rub == Decimal("5000")
    assert line.total_cost_rub == Decimal("2500.00")
    assert calculate_repair_customer_amount(order) == Decimal("5000.00")


def test_custom_price_multi_item_and_legacy_unknown(repair_data):
    user, _location, stock = repair_data
    _part_a, lot_a = stock("A", "1000")
    _part_b, lot_b = stock("B", "2500")
    order = create_repair_order(customer_name="Клиент", by=user)
    add_stock_lot_to_repair_order(
        order, lot_a, Decimal("2"), customer_unit_price_rub="1000", by=user
    )
    add_stock_lot_to_repair_order(
        order, lot_b, Decimal("1"), customer_unit_price_rub="2500", by=user
    )
    complete_repair_order(order, by=user)
    assert calculate_repair_customer_amount(order) == Decimal("4500.00")

    _part_c, lot_c = stock("Старый", "9000")
    legacy = _completed(user, lot_c, "1")
    legacy.lines.update(customer_unit_price_rub=None)
    assert repair_customer_amounts([legacy])[legacy.pk] is None


def test_partial_return_reduces_customer_amount_and_cancel_restores_it(repair_data):
    user, location, stock = repair_data
    _part, lot = stock("Болт", "1000")
    order = _completed(user, lot, "5")
    order.refresh_from_db()
    line = order.lines.get()
    returned = create_return(source=order, by=user)
    add_repair_line_return(
        returned,
        line,
        Decimal("2"),
        to_location=location,
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=user,
    )
    complete_return(returned, by=user)
    assert calculate_repair_customer_amount(order) == Decimal("3000.00")
    cancel_return(returned, by=user, reason="Ошибочный возврат")
    assert calculate_repair_customer_amount(order) == Decimal("5000.00")


def test_missing_price_is_unknown_not_zero(repair_data):
    user, _location, stock = repair_data
    _part, lot = stock("Без цены", None)
    order = _completed(user, lot, "1")
    assert order.lines.get().customer_unit_price_rub is None
    assert calculate_repair_customer_amount(order) is None
