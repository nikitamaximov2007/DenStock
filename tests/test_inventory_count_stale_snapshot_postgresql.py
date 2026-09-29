"""Count completion racing a sale on the same lot, on a real PostgreSQL.

Whatever the order, sold units must never come back: either the count
completes first (no change: it counted 5 of 5) and the sale then takes 2, or
the sale lands first and the now-stale count is refused. The lot ends at 3
either way - never at 5.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockLot
from apps.inventory.services import (
    InventoryError,
    check_stock_balance,
    create_stock_lot,
    receive_stock_lot,
    sell_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.stocktaking.services import (
    StocktakingError,
    add_stock_lot_count_line,
    complete_inventory_count,
    create_inventory_count,
    update_counted_quantity,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

pytestmark = [
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL concurrency integration test",
    ),
]


@pytest.fixture
def scene():
    admin = get_user_model().objects.create_superuser(username="count-race", password="x-12345")
    part = PartType.objects.create(
        name="Болт", category=Category.objects.create(name="Гонки"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    cell = StorageLocation.objects.create(
        name="Ячейка", code="S07-D05-C01", storage_allowed=True, is_active=True
    )
    batch = Batch.objects.create(supplier=Supplier.objects.create(name="П"), shipping_cost=0)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("5"), unit_cost_currency=Decimal("5")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = receive_stock_lot(create_stock_lot(line, cell, Decimal("5")), by=admin)
    doc = create_inventory_count(scope_location=cell, by=admin)
    count_line = add_stock_lot_count_line(doc, lot, by=admin)
    update_counted_quantity(count_line, Decimal("5"), by=admin)
    return {"admin": admin, "lot": lot, "doc": doc}


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


@pytest.mark.parametrize("attempt", range(3))
def test_count_completion_racing_a_sale_never_resurrects_units(scene, attempt):
    admin, lot, doc = scene["admin"], scene["lot"], scene["doc"]

    completion, sale = _race(
        lambda: complete_inventory_count(doc, by=admin),
        lambda: sell_stock_lot(lot, Decimal("2"), by=admin),
    )

    assert not isinstance(sale, Exception) or isinstance(sale, InventoryError), sale
    assert not isinstance(completion, Exception) or isinstance(
        completion, StocktakingError
    ), completion
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("3")
    assert check_stock_balance() == []
