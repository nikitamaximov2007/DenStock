"""Races around a lot's first movement, on a real PostgreSQL.

The direct-edit guard ("no movements yet") and the receipt through a status
button are both check-then-act. Here two operators hit the same lot at once;
whatever the order, the lot must stay equal to its movement journal and the
balance cache, and a lot is received exactly once.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    change_stock_lot_status,
    check_stock_balance,
    create_stock_lot,
    receive_stock_lot,
    update_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

pytestmark = [
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL concurrency integration test",
    ),
]

RECEIPT = StockMovement.MovementType.RECEIVE_LOT


@pytest.fixture
def world():
    admin = get_user_model().objects.create_superuser(username="lot-race", password="x-12345")
    part = PartType.objects.create(
        name="Болт", category=Category.objects.create(name="Гонки"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    cell = StorageLocation.objects.create(
        name="Ячейка", code="S07-D01-C01", storage_allowed=True, is_active=True
    )
    batch = Batch.objects.create(supplier=Supplier.objects.create(name="П"), shipping_cost=0)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("10"), unit_cost_currency=Decimal("5")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return {"admin": admin, "cell": cell, "lot": create_stock_lot(line, cell, Decimal("10"))}


def _race(*calls):
    barrier = Barrier(len(calls))

    def runner(call):
        close_old_connections()
        try:
            barrier.wait(20)
            return call()
        except Exception as exc:  # noqa: BLE001 - the race outcome is the assertion
            return exc
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [f.result() for f in [pool.submit(runner, call) for call in calls]]


def _assert_journal_matches(lot):
    lot.refresh_from_db()
    receipts = list(StockMovement.objects.filter(stock_lot=lot, movement_type=RECEIPT))
    assert len(receipts) == 1
    assert receipts[0].quantity == lot.quantity
    assert check_stock_balance() == []


def test_double_available_click_receives_once(world):
    lot, admin = world["lot"], world["admin"]

    results = _race(
        lambda: change_stock_lot_status(lot, StockLot.Status.AVAILABLE, by=admin),
        lambda: change_stock_lot_status(lot, StockLot.Status.AVAILABLE, by=admin),
    )

    assert not [r for r in results if isinstance(r, Exception)], results
    lot.refresh_from_db()
    assert lot.status == StockLot.Status.AVAILABLE
    _assert_journal_matches(lot)


def test_status_button_racing_accept_button_receives_once(world):
    lot, admin = world["lot"], world["admin"]

    results = _race(
        lambda: change_stock_lot_status(lot, StockLot.Status.QUARANTINE, by=admin),
        lambda: receive_stock_lot(lot, by=admin),
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert all(isinstance(e, InventoryError) for e in errors), errors
    _assert_journal_matches(lot)


@pytest.mark.parametrize("attempt", range(3))
def test_direct_edit_racing_receipt_never_leaves_stock_off_the_journal(world, attempt):
    lot, admin, cell = world["lot"], world["admin"], world["cell"]

    results = _race(
        lambda: update_stock_lot(lot, location=cell, quantity=Decimal("6")),
        lambda: receive_stock_lot(lot, by=admin),
    )

    edit, receipt = results
    assert not isinstance(receipt, Exception), receipt
    # Either the edit landed first (and was received as 6) or it was refused.
    assert not isinstance(edit, Exception) or isinstance(edit, InventoryError), edit
    _assert_journal_matches(lot)
