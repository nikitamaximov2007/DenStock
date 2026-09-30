"""Cell rebind races on a real PostgreSQL: never two live bindings, never lost stock."""
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
    move_stock_lot,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import create_location
from apps.warehouse.models import (
    StorageLocation,
    StorageLocationAlias,
    StorageLocationRenameHistory,
)
from apps.warehouse.services import StorageLocationRenameError, rebind_storage_cell

pytestmark = [
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="PostgreSQL concurrency integration test"
    ),
]


@pytest.fixture
def scene():
    admin = get_user_model().objects.create_superuser(username="rebind-race", password="x-12345")
    part = PartType.objects.create(
        name="Болт", category=Category.objects.create(name="Гонка"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    cells = [create_location(code) for code in ("S03-D02-C08", "S03-D02-C09", "S03-D02-C01")]
    batch = Batch.objects.create(supplier=Supplier.objects.create(name="П"), shipping_cost=0)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("5"), unit_cost_currency=Decimal("5")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = receive_stock_lot(create_stock_lot(line, cells[0], Decimal("5")), by=admin)
    return {"admin": admin, "cells": cells, "lot": lot}


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


def _rebind(scene, cell, target):
    return lambda: rebind_storage_cell(
        StorageLocation.objects.get(pk=cell.pk), new_code=target,
        expected_code=cell.code, by=scene["admin"],
    )


def _one_winner(results):
    wins = [r for r in results if isinstance(r, StorageLocation)]
    losses = [r for r in results if isinstance(r, StorageLocationRenameError)]
    assert len(wins) == 1 and len(losses) == len(results) - 1, results
    return wins[0]


def _no_duplicate_live_address():
    codes = list(StorageLocation.objects.values_list("code", flat=True))
    assert len(codes) == len(set(code.upper() for code in codes))
    assert not StorageLocationAlias.objects.filter(is_active=True).exists()


@pytest.mark.parametrize("attempt", range(2))
def test_same_cell_to_two_targets_has_one_winner(scene, attempt):
    cell = scene["cells"][0]

    winner = _one_winner(_race(
        _rebind(scene, cell, "S03-D02-C07"), _rebind(scene, cell, "S03-D05-C01"),
    ))

    cell.refresh_from_db()
    assert cell.code == winner.code
    assert StorageLocationRenameHistory.objects.filter(location=cell).count() == 1
    assert StorageLocationAlias.objects.filter(location=cell).count() == 1
    assert StockLot.objects.get(pk=scene["lot"].pk).location_id == cell.pk
    _no_duplicate_live_address()
    assert check_stock_balance() == []


@pytest.mark.parametrize("attempt", range(2))
def test_two_cells_to_the_same_target_has_one_winner(scene, attempt):
    first, second = scene["cells"][0], scene["cells"][1]

    winner = _one_winner(_race(
        _rebind(scene, first, "S03-D02-C07"), _rebind(scene, second, "S03-D02-C07"),
    ))

    assert StorageLocation.objects.filter(code="S03-D02-C07").get().pk == winner.pk
    loser = second if winner.pk == first.pk else first
    loser.refresh_from_db()
    assert loser.code in ("S03-D02-C08", "S03-D02-C09")
    assert not StorageLocationRenameHistory.objects.filter(location=loser).exists()
    _no_duplicate_live_address()
    assert check_stock_balance() == []


@pytest.mark.parametrize("direction", ["out", "in"])
def test_stock_move_racing_the_rebind_loses_nothing(scene, direction):
    cell, other = scene["cells"][0], scene["cells"][2]
    lot = scene["lot"]
    if direction == "in":
        move_stock_lot(lot, other, by=scene["admin"])
    target = other if direction == "out" else cell

    rebind, moved = _race(
        _rebind(scene, cell, "S03-D02-C07"),
        lambda: move_stock_lot(StockLot.objects.get(pk=lot.pk), target, by=scene["admin"]),
    )

    assert isinstance(rebind, StorageLocation), rebind
    assert not isinstance(moved, Exception) or isinstance(moved, InventoryError), moved
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")
    assert lot.location_id in (cell.pk, other.pk)
    cell.refresh_from_db()
    assert cell.code == "S03-D02-C07"
    _no_duplicate_live_address()
    assert check_stock_balance() == []


def test_two_cells_into_a_new_drawer_share_one_created_parent(scene):
    first, second = scene["cells"][0], scene["cells"][1]

    results = _race(
        _rebind(scene, first, "S03-D06-C01"), _rebind(scene, second, "S03-D06-C02"),
    )

    assert all(isinstance(r, StorageLocation) for r in results), results
    drawers = StorageLocation.objects.filter(code="S03-D06")
    assert drawers.count() == 1
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.parent_id == second.parent_id == drawers.get().pk
    _no_duplicate_live_address()
