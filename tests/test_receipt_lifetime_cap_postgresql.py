"""AUD-01 on PostgreSQL 16: concurrent intake on one batch line, forced.

The batch line row is the lock that serializes intake. The holder takes it,
waits until pg_stat_activity shows the contender blocked on a lock, receives
+2 and commits; the contender then re-reads the committed history under the
same lock and is refused. A test fails if the contender was never seen
blocked, so the race really executes.
"""
import time
from decimal import Decimal
from threading import Event, Thread

import pytest
from django.db import close_old_connections, connection, transaction

from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    create_stock_lot,
    receive_stock_lot,
    remaining_qty,
    update_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.warehouse.models import StorageLocation
from tests.test_piece_stock_boundary_postgresql import (  # noqa: F401
    _backend_pid,
    _waits_on_lock,
    units,
)

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="PostgreSQL 16 concurrency qualification"
    ),
]


@pytest.fixture
def line_8_of_10(units, public_catalog):  # noqa: F811
    part = public_catalog.part("Ремень", article="PGCAP-1", price="100")
    batch = Batch.objects.create(supplier=public_catalog.supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("10"), unit_cost_currency=Decimal("1")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, public_catalog.user)
    line = BatchLine.objects.select_related("batch", "part_type").get(pk=line.pk)
    cells = [
        StorageLocation.objects.create(
            name=f"Cap {n}", code=f"S09-D03-C0{n}", storage_allowed=True, is_active=True
        )
        for n in range(1, 4)
    ]
    receive_stock_lot(create_stock_lot(line, cells[0], Decimal("8")))
    return {"line": line, "cells": cells}


def _race_on_line(line_pk, *, holder, contender):
    locked, contender_pid = Event(), {}
    outcome = {"holder": None, "contender": None, "waited": False}

    def run(fn, key):
        try:
            outcome[key] = ("ok", fn())
        except Exception as exc:  # each side's outcome is asserted by the test
            outcome[key] = ("error", exc)

    def run_contender():
        close_old_connections()
        try:
            contender_pid["pid"] = _backend_pid()
            locked.wait(20)
            run(contender, "contender")
        finally:
            close_old_connections()

    def run_holder():
        close_old_connections()
        try:
            with transaction.atomic():
                BatchLine.objects.select_for_update().get(pk=line_pk)
                locked.set()
                while "pid" not in contender_pid:
                    time.sleep(0.01)
                outcome["waited"] = _waits_on_lock(contender_pid["pid"])
                run(holder, "holder")
        finally:
            close_old_connections()

    threads = [Thread(target=run_contender), Thread(target=run_holder)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert outcome["waited"], "the contender never blocked on the batch line lock"
    return outcome


def test_two_concurrent_plus_two_receipts_on_8_of_10_let_exactly_one_through(line_8_of_10):
    line, cells = line_8_of_10["line"], line_8_of_10["cells"]

    outcome = _race_on_line(
        line.pk,
        holder=lambda: receive_stock_lot(create_stock_lot(line, cells[1], Decimal("2"))),
        contender=lambda: receive_stock_lot(create_stock_lot(line, cells[2], Decimal("2"))),
    )

    assert outcome["holder"][0] == "ok"
    assert outcome["contender"][0] == "error"
    assert isinstance(outcome["contender"][1], InventoryError)
    assert "можно принять ещё 0" in str(outcome["contender"][1])
    assert sorted(
        StockLot.objects.filter(batch_line=line).values_list("quantity", flat=True)
    ) == [Decimal("2"), Decimal("8")]
    assert not StockLot.objects.filter(batch_line=line, location=cells[2]).exists()
    assert StockMovement.objects.filter(batch_line=line).count() == 2
    assert remaining_qty(line) == Decimal("0")


def test_editing_a_pending_lot_upward_waits_and_respects_the_other_receipt(line_8_of_10):
    line, cells = line_8_of_10["line"], line_8_of_10["cells"]
    pending = create_stock_lot(line, cells[2], Decimal("1"))  # holds 1 of the last 2

    outcome = _race_on_line(
        line.pk,
        holder=lambda: create_stock_lot(line, cells[1], Decimal("1")),
        contender=lambda: update_stock_lot(pending, location=cells[2], quantity=Decimal("2")),
    )

    assert outcome["holder"][0] == "ok"
    assert outcome["contender"][0] == "error"
    assert StockLot.objects.get(pk=pending.pk).quantity == Decimal("1")
    assert remaining_qty(line) == Decimal("0")


def test_a_sale_committing_while_a_receipt_waits_does_not_reopen_capacity(
    line_8_of_10, public_catalog
):
    from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
    from tests.customs_support import remember_customs

    line, cells = line_8_of_10["line"], line_8_of_10["cells"]
    receive_stock_lot(create_stock_lot(line, cells[1], Decimal("2")))  # 10 of 10
    lot = StockLot.objects.get(batch_line=line, location=cells[0])
    remember_customs(line.part_type)
    sale = create_sale(customer_name="Клиент", by=public_catalog.user)
    add_stock_lot_to_sale(sale, lot, Decimal("2"), unit_price=Decimal("100"))

    outcome = _race_on_line(
        line.pk,
        holder=lambda: complete_sale(sale, by=public_catalog.user),  # 8 left on the shelf
        contender=lambda: create_stock_lot(line, cells[2], Decimal("2")),
    )

    assert outcome["holder"][0] == "ok"
    assert outcome["contender"][0] == "error"
    assert not StockLot.objects.filter(batch_line=line, location=cells[2]).exists()
    assert remaining_qty(line) == Decimal("0")
