"""Two operators press "Продать из резерва" at the same instant.

The reservation stays ACTIVE until the sale is completed, so without a
serialized check both requests used to create their own draft. Both now lock
the same reservation row first; exactly one draft exists afterwards and both
callers get it.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Sale, SaleLine
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
    get_or_create_sale_from_reservation,
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
def reservation():
    admin = get_user_model().objects.create_superuser(username="resv-race", password="x-12345")
    part = PartType.objects.create(
        name="Ремень", category=Category.objects.create(name="Гонки"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("1000"),
    )
    cell = StorageLocation.objects.create(
        name="Ячейка", code="S07-D04-C01", storage_allowed=True, is_active=True
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
    reservation = create_reservation(customer_name="Иванов", by=admin)
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=admin)
    return activate_reservation(reservation, by=admin), admin


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
def test_concurrent_sale_from_reservation_creates_one_draft(reservation, attempt):
    resv, admin = reservation

    results = _race(
        lambda: get_or_create_sale_from_reservation(resv, by=admin),
        lambda: get_or_create_sale_from_reservation(resv, by=admin),
    )

    assert not [r for r in results if isinstance(r, Exception)], results
    (first, created_a), (second, created_b) = results
    assert first.pk == second.pk
    assert sorted([created_a, created_b]) == [False, True]
    assert Sale.objects.filter(reservation=resv).count() == 1
    assert SaleLine.objects.count() == 1
