"""Races between a direct location edit and the item's first movement.

The direct-edit guard ("no movements yet") is check-then-act. Here an
operator edits the item's location directly while another operator receives
or moves it at the same instant - whatever the order, the item must end up
consistent with its own movement journal, never a silent teleport with no
record.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockMovement
from apps.inventory.services import (
    InventoryError,
    check_stock_balance,
    create_part_items,
    receive_part_item,
    update_part_item,
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

RECEIPT = StockMovement.MovementType.RECEIVE_ITEM


@pytest.fixture
def world():
    admin = get_user_model().objects.create_superuser(username="edit-race", password="x-12345")
    part = PartType.objects.create(
        name="Насос", category=Category.objects.create(name="Гонки"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.SERIAL,
    )
    cell = StorageLocation.objects.create(
        name="Ячейка", code="S07-D03-C01", storage_allowed=True, is_active=True
    )
    other = StorageLocation.objects.create(
        name="Ячейка 2", code="S07-D03-C02", storage_allowed=True, is_active=True
    )
    batch = Batch.objects.create(supplier=Supplier.objects.create(name="П"), shipping_cost=0)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("1"), unit_cost_currency=Decimal("5")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    item = create_part_items(line, 1, current_location=cell)[0]
    return {"admin": admin, "cell": cell, "other": other, "item": item}


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


def _assert_consistent(item):
    item.refresh_from_db()
    receipts = list(StockMovement.objects.filter(part_item=item, movement_type=RECEIPT))
    assert len(receipts) <= 1
    assert check_stock_balance() == []


@pytest.mark.parametrize("attempt", range(3))
def test_direct_edit_racing_receipt_never_leaves_stock_off_the_journal(world, attempt):
    item, admin, other = world["item"], world["admin"], world["other"]

    results = _race(
        lambda: update_part_item(item, current_location=other),
        lambda: receive_part_item(item, by=admin),
    )

    edit, receipt = results
    assert not isinstance(receipt, Exception), receipt
    # Either the edit landed first (item received where the edit put it) or
    # it was refused once the receipt won the race - never a silent
    # location change alongside an unrelated receipt.
    assert not isinstance(edit, Exception) or isinstance(edit, InventoryError), edit
    item.refresh_from_db()
    if not isinstance(edit, Exception):
        assert item.current_location == other
    _assert_consistent(item)
