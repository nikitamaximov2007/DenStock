"""An inventory count is never applied over stock that changed after it.

A count line snapshots the lot (`expected_quantity`); the operator counts;
completion used to set the lot to `counted` by adjusting against the LIVE
quantity. If units left in between (count 5, sell 2, live 3), completion
booked ADJUST_IN +2 and resurrected sold units - phantom stock. Completion now
refuses the whole document when any lot changed after its snapshot, and the
operator re-adds and recounts those lines.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockBalance, StockLot, StockMovement
from apps.inventory.services import (
    adjust_stock_lot_quantity,
    check_stock_balance,
    create_stock_lot,
    move_stock_lot,
    receive_stock_lot,
    sell_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.stocktaking.models import InventoryCountDocument
from apps.stocktaking.services import (
    StocktakingError,
    add_stock_lot_count_line,
    complete_inventory_count,
    create_inventory_count,
    remove_count_line,
    update_counted_quantity,
)
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location

ADJUST_TYPES = (StockMovement.MovementType.ADJUST_IN, StockMovement.MovementType.ADJUST_OUT)


@pytest.fixture
def env(db, django_user_model):
    Group.objects.all()
    admin = django_user_model.objects.create_superuser(username="count-admin", password="x-12345")
    part = PartType.objects.create(
        name="Болт", category=Category.objects.create(name="Счёт"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    return {
        "admin": admin, "part": part, "supplier": Supplier.objects.create(name="Поставщик"),
        "cell": get_or_create_location("S08-D01-C01", name="Ячейка A"),
        "other": get_or_create_location("S08-D01-C02", name="Ячейка B"),
    }


def _lot(env, quantity="5", *, received=True):
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=env["part"], quantity=Decimal(quantity),
        unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["cell"], Decimal(quantity))
    return receive_stock_lot(lot, by=env["admin"]) if received else lot


def _counted(env, lot, counted):
    doc = create_inventory_count(scope_location=env["cell"], by=env["admin"])
    line = add_stock_lot_count_line(doc, lot, by=env["admin"])
    update_counted_quantity(line, Decimal(counted), by=env["admin"])
    return doc, line


def _adjustments():
    return StockMovement.objects.filter(movement_type__in=ADJUST_TYPES).count()


def _assert_refused(env, doc, lot, quantity):
    before = _adjustments()
    with pytest.raises(StocktakingError, match="Остаток изменился"):
        complete_inventory_count(doc, by=env["admin"])
    doc.refresh_from_db()
    lot.refresh_from_db()
    assert doc.status == InventoryCountDocument.Status.DRAFT
    assert lot.quantity == Decimal(quantity)
    assert _adjustments() == before
    assert check_stock_balance() == []


# --- Unchanged stock: the count completes exactly as before --------------------


def test_unchanged_stock_completes_and_adjusts_to_the_count(env):
    lot = _lot(env)
    doc, _line = _counted(env, lot, "4")

    complete_inventory_count(doc, by=env["admin"])

    lot.refresh_from_db()
    assert lot.quantity == Decimal("4")
    assert StockMovement.objects.filter(
        stock_lot=lot, movement_type=StockMovement.MovementType.ADJUST_OUT
    ).count() == 1
    balance = StockBalance.objects.get(batch_line=lot.batch_line, location=env["cell"])
    assert balance.quantity_physical == Decimal("4")
    assert check_stock_balance() == []


# --- Stock changed after the snapshot: completion refused --------------------


def test_sale_after_count_is_not_resurrected(env):
    lot = _lot(env)
    doc, _line = _counted(env, lot, "5")
    sell_stock_lot(lot, Decimal("2"), by=env["admin"])

    _assert_refused(env, doc, lot, "3")


def test_receipt_after_count_is_refused(env):
    lot = _lot(env, received=False)
    doc, _line = _counted(env, lot, "5")
    receive_stock_lot(lot, by=env["admin"])

    _assert_refused(env, doc, lot, "5")


def test_move_to_another_cell_after_count_is_refused(env):
    lot = _lot(env)
    doc, _line = _counted(env, lot, "4")
    move_stock_lot(lot, env["other"], by=env["admin"])

    _assert_refused(env, doc, lot, "5")
    lot.refresh_from_db()
    assert lot.location == env["other"]


def test_changes_that_net_to_the_same_quantity_are_still_refused(env):
    """Sell 2, get 2 back: same number, but the count may sit in between."""
    lot = _lot(env)
    doc, _line = _counted(env, lot, "3")
    sell_stock_lot(lot, Decimal("2"), by=env["admin"])
    adjust_stock_lot_quantity(lot, Decimal("2"), by=env["admin"], comment="нашлись")
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")

    _assert_refused(env, doc, lot, "5")


# --- Retry: recount the refused lines, then it completes ---------------------


def test_recounting_a_refused_line_completes_without_phantom_stock(env):
    lot = _lot(env)
    doc, line = _counted(env, lot, "5")
    sell_stock_lot(lot, Decimal("2"), by=env["admin"])
    with pytest.raises(StocktakingError):
        complete_inventory_count(doc, by=env["admin"])

    remove_count_line(line, by=env["admin"])
    fresh = add_stock_lot_count_line(doc, lot, by=env["admin"])
    assert fresh.expected_quantity == Decimal("3")
    update_counted_quantity(fresh, Decimal("3"), by=env["admin"])
    complete_inventory_count(doc, by=env["admin"])

    lot.refresh_from_db()
    doc.refresh_from_db()
    assert doc.status == InventoryCountDocument.Status.COMPLETED
    assert lot.quantity == Decimal("3")
    assert _adjustments() == 0
    assert check_stock_balance() == []
    with pytest.raises(StocktakingError):
        complete_inventory_count(doc, by=env["admin"])


def test_one_stale_line_blocks_the_whole_document(env):
    """Completion stays all-or-nothing, exactly as before."""
    untouched = _lot(env)
    changed = _lot(env)
    doc = create_inventory_count(scope_location=env["cell"], by=env["admin"])
    for lot, counted in ((untouched, "4"), (changed, "5")):
        line = add_stock_lot_count_line(doc, lot, by=env["admin"])
        update_counted_quantity(line, Decimal(counted), by=env["admin"])
    sell_stock_lot(changed, Decimal("1"), by=env["admin"])

    with pytest.raises(StocktakingError, match=f"#{changed.pk}"):
        complete_inventory_count(doc, by=env["admin"])

    untouched.refresh_from_db()
    assert untouched.quantity == Decimal("5")
    assert StockLot.objects.get(pk=changed.pk).quantity == Decimal("4")
