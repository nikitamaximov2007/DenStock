"""Races around a serialized item's first movement, on a real PostgreSQL.

Two operators hit the same RECEIVING item at once - whatever the order, the
item must stay equal to its movement journal and the balance cache, and it is
received exactly once.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import PartItem, StockMovement
from apps.inventory.services import (
    InventoryError,
    change_part_item_status,
    check_stock_balance,
    create_part_items,
    receive_part_item,
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
    admin = get_user_model().objects.create_superuser(username="item-race", password="x-12345")
    part = PartType.objects.create(
        name="Насос", category=Category.objects.create(name="Гонки"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.SERIAL,
    )
    cell = StorageLocation.objects.create(
        name="Ячейка", code="S07-D02-C01", storage_allowed=True, is_active=True
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
    return {"admin": admin, "cell": cell, "item": item}


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


def _assert_journal_matches(item):
    item.refresh_from_db()
    receipts = list(StockMovement.objects.filter(part_item=item, movement_type=RECEIPT))
    assert len(receipts) == 1
    assert check_stock_balance() == []


def test_double_available_click_receives_once(world):
    item, admin = world["item"], world["admin"]

    results = _race(
        lambda: change_part_item_status(item, PartItem.Status.AVAILABLE, by=admin),
        lambda: change_part_item_status(item, PartItem.Status.AVAILABLE, by=admin),
    )

    assert not [r for r in results if isinstance(r, Exception)], results
    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    _assert_journal_matches(item)


def test_status_button_racing_accept_button_receives_once(world):
    item, admin = world["item"], world["admin"]

    results = _race(
        lambda: change_part_item_status(item, PartItem.Status.QUARANTINE, by=admin),
        lambda: receive_part_item(item, by=admin),
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert all(isinstance(e, InventoryError) for e in errors), errors
    _assert_journal_matches(item)


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_available_and_quarantine_receive_exactly_once(world, attempt):
    item, admin = world["item"], world["admin"]

    results = _race(
        lambda: change_part_item_status(item, PartItem.Status.AVAILABLE, by=admin),
        lambda: change_part_item_status(item, PartItem.Status.QUARANTINE, by=admin),
    )

    assert not [r for r in results if isinstance(r, Exception)], results
    _assert_journal_matches(item)
