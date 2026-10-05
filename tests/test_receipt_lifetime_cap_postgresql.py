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


def _race_under_exclusive_line_lock(line_pk, operations):
    """Hold a real PostgreSQL row lock until every contender is observed waiting."""
    locked, release = Event(), Event()
    pids, outcomes = {}, {}

    def blocker():
        close_old_connections()
        try:
            with transaction.atomic():
                BatchLine.objects.select_for_update().get(pk=line_pk)
                locked.set()
                if not release.wait(20):
                    raise AssertionError("test did not release the line lock")
        finally:
            close_old_connections()

    def contender(key, fn):
        close_old_connections()
        try:
            pids[key] = _backend_pid()
            outcomes[key] = ("ok", fn())
        except Exception as exc:  # asserted after every thread joins
            outcomes[key] = ("error", exc)
        finally:
            close_old_connections()

    blocker_thread = Thread(target=blocker)
    blocker_thread.start()
    assert locked.wait(20), "exclusive line lock was not acquired"
    threads = [Thread(target=contender, args=(key, fn)) for key, fn in operations.items()]
    for thread in threads:
        thread.start()
    for key in operations:
        deadline = time.monotonic() + 20
        while key not in pids and time.monotonic() < deadline:
            time.sleep(0.01)
        assert key in pids, f"{key} did not connect to PostgreSQL"
        assert _waits_on_lock(pids[key]), f"{key} never waited on the held batch-line lock"
    release.set()
    for thread in [*threads, blocker_thread]:
        thread.join(60)
        assert not thread.is_alive(), "PostgreSQL contention test deadlocked"
    assert all(result[0] == "ok" for result in outcomes.values()), outcomes
    return outcomes


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


def test_sale_and_receipt_wait_on_real_pg_lock_without_over_receipt(
    line_8_of_10, public_catalog
):
    from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
    from tests.customs_support import remember_customs

    line, cells = line_8_of_10["line"], line_8_of_10["cells"]
    lot = StockLot.objects.get(batch_line=line, location=cells[0])
    remember_customs(line.part_type)
    sale = create_sale(customer_name="Клиент", by=public_catalog.user)
    add_stock_lot_to_sale(sale, lot, Decimal("2"), unit_price=Decimal("100"))
    outcomes = _race_under_exclusive_line_lock(
        line.pk,
        {
            "sale": lambda: complete_sale(sale, by=public_catalog.user),
            "receipt": lambda: receive_stock_lot(
                create_stock_lot(line, cells[2], Decimal("2"))
            ),
        },
    )
    assert set(outcomes) == {"sale", "receipt"}
    assert remaining_qty(line) == Decimal("0")
    assert StockMovement.objects.filter(batch_line=line).count() == 3


def test_reconciliation_and_receipt_wait_on_real_pg_lock_without_stale_capacity(
    line_8_of_10
):
    from apps.inventory.services import adjust_stock_lot_quantity

    line, cells = line_8_of_10["line"], line_8_of_10["cells"]
    lot = StockLot.objects.get(batch_line=line, location=cells[0])
    outcomes = _race_under_exclusive_line_lock(
        line.pk,
        {
            "reconciliation": lambda: adjust_stock_lot_quantity(
                lot, Decimal("-1"), comment="Пересчёт"
            ),
            "receipt": lambda: receive_stock_lot(
                create_stock_lot(line, cells[2], Decimal("2"))
            ),
        },
    )
    assert set(outcomes) == {"reconciliation", "receipt"}
    assert remaining_qty(line) == Decimal("0")
    assert StockMovement.objects.filter(batch_line=line).count() == 3


def test_transfer_and_receipt_wait_on_real_pg_lock_without_over_receipt(line_8_of_10):
    from apps.inventory.services import perform_stock_transfer

    line, cells = line_8_of_10["line"], line_8_of_10["cells"]
    outcomes = _race_under_exclusive_line_lock(
        line.pk,
        {
            "transfer": lambda: perform_stock_transfer(
                part=line.part_type, from_location=cells[0], to_location=cells[1],
                quantity="2", stock_state=StockLot.Status.AVAILABLE, token="pg-line-lock-transfer",
            ),
            "receipt": lambda: receive_stock_lot(
                create_stock_lot(line, cells[2], Decimal("2"))
            ),
        },
    )
    assert set(outcomes) == {"transfer", "receipt"}
    assert remaining_qty(line) == Decimal("0")
    assert StockMovement.objects.filter(batch_line=line).count() == 3


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



# --- A receipt reading the line vs. ledger writers on the same line ---------------------
#
# A sale, adjustment or transfer never takes the batch line lock. The receipt
# holds the line FOR NO KEY UPDATE, so their commit-time foreign key checks
# (FOR KEY SHARE) do not wait for it: they commit while the receipt is between
# its reads of the line's lots and movements, and the receipt must still decide
# on one consistent picture (the movement-set bracket in line_provenance_detail).
# With a FOR UPDATE line lock a transfer deadlocked with the receipt instead:
# the transfer held the part card (preferred cell) and waited for the line at
# COMMIT, while the receipt held the line and waited for the part card.


def _receipt_paused_between_reads(receipt, writer):
    """Run `receipt` in its own transaction, paused right after it read the
    line's lots; meanwhile `writer` runs and must commit without waiting."""
    paused, resume = Event(), Event()
    outcome = {"receipt": None, "writer": None}

    def pause_after_lots_read(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if (
            not paused.is_set()
            and sql.startswith("SELECT")
            and 'FROM "inventory_stocklot"' in sql
            and '"inventory_stocklot"."batch_line_id" =' in sql
            # The old predicate paused on lots_before (a pk-only ID probe).
            # This is the actual model-row read consumed by _read_line, where
            # provenance snapshots the lot state before reading its ledger.
            and '"inventory_stocklot"."note"' in sql
        ):
            paused.set()
            resume.wait(20)
        return result

    def run(fn, key, wrapper=None):
        close_old_connections()
        try:
            if wrapper is None:
                outcome[key] = ("ok", fn())
            else:
                with connection.execute_wrapper(wrapper), transaction.atomic():
                    outcome[key] = ("ok", fn())
        except Exception as exc:  # each side's outcome is asserted by the test
            outcome[key] = ("error", exc)
        finally:
            close_old_connections()

    receipt_thread = Thread(target=run, args=(receipt, "receipt", pause_after_lots_read))
    receipt_thread.start()
    assert paused.wait(20), "the receipt never reached its read of the line's lots"
    writer_thread = Thread(target=run, args=(writer, "writer"))
    writer_thread.start()
    writer_thread.join(10)
    writer_waited = writer_thread.is_alive()
    resume.set()
    for thread in (writer_thread, receipt_thread):
        thread.join(60)
    assert not writer_waited, f"the writer waited for the receipt: {outcome}"
    return outcome


def test_reread_bracket_negative_control_changes_the_sale_race_decision(
    line_8_of_10, public_catalog, monkeypatch
):
    """The correct reread accepts exact remaining capacity; one stale attempt refuses."""
    from apps.inventory import lot_provenance
    from apps.inventory.services import InventoryError
    from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
    from tests.customs_support import remember_customs

    remember_customs(line_8_of_10["line"].part_type)
    cell = line_8_of_10["cells"][2]
    line, lot = _legacy_line(line_8_of_10, public_catalog)
    sale = create_sale(customer_name="Клиент", by=public_catalog.user)
    add_stock_lot_to_sale(sale, lot, Decimal("2"), unit_price=Decimal("100"))
    correct = _receipt_paused_between_reads(
        lambda: receive_stock_lot(create_stock_lot(line, cell, Decimal("4"))),
        lambda: complete_sale(sale, by=public_catalog.user),
    )
    assert correct["writer"][0] == "ok", correct
    assert correct["receipt"][0] == "ok", correct
    assert remaining_qty(line) == Decimal("0")

    line, lot = _legacy_line(line_8_of_10, public_catalog, cell_code="S09-D03-C09")
    sale = create_sale(customer_name="Клиент", by=public_catalog.user)
    add_stock_lot_to_sale(sale, lot, Decimal("2"), unit_price=Decimal("100"))
    original = lot_provenance.line_provenance_detail

    def one_attempt(target_line, **kwargs):
        return original(target_line, attempts=1, **kwargs)

    monkeypatch.setattr(lot_provenance, "line_provenance_detail", one_attempt)
    outcome = _receipt_paused_between_reads(
        lambda: receive_stock_lot(create_stock_lot(line, cell, Decimal("4"))),
        lambda: complete_sale(sale, by=public_catalog.user),
    )

    assert outcome["writer"][0] == "ok", outcome
    assert outcome["receipt"][0] == "error", outcome
    assert isinstance(outcome["receipt"][1], InventoryError)
    assert not StockLot.objects.filter(batch_line=line, location=cell).exists()


def _legacy_line(line_8_of_10, public_catalog, *, cell_code="S09-D03-C07"):
    """A second line whose only lot was received by the pre-108b5ad status flip:
    its intake is rebuilt from the ledger, so a half-seen sale would matter."""
    part = line_8_of_10["line"].part_type
    batch = Batch.objects.create(supplier=public_catalog.supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("10"), unit_cost_currency=Decimal("1")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, public_catalog.user)
    line = BatchLine.objects.select_related("batch", "part_type").get(pk=line.pk)
    cell = StorageLocation.objects.create(
        name="Cap legacy", code=cell_code, storage_allowed=True, is_active=True
    )
    lot = create_stock_lot(line, cell, Decimal("6"))
    StockLot.objects.filter(pk=lot.pk).update(status=StockLot.Status.AVAILABLE)
    lot.refresh_from_db()
    return line, lot


def test_a_sale_committed_between_the_receipt_reads_is_not_misread(
    line_8_of_10, public_catalog
):
    from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
    from tests.customs_support import remember_customs

    remember_customs(line_8_of_10["line"].part_type)
    line, lot = _legacy_line(line_8_of_10, public_catalog)
    sale = create_sale(customer_name="Клиент", by=public_catalog.user)
    add_stock_lot_to_sale(sale, lot, Decimal("2"), unit_price=Decimal("100"))
    cell = line_8_of_10["cells"][2]

    outcome = _receipt_paused_between_reads(
        lambda: receive_stock_lot(create_stock_lot(line, cell, Decimal("4"))),  # 6 + 4
        lambda: complete_sale(sale, by=public_catalog.user),
    )

    assert outcome["writer"][0] == "ok", outcome
    assert outcome["receipt"][0] == "ok", outcome  # the half-seen sale did not block it
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("4")
    assert remaining_qty(line) == Decimal("0")  # and did not reopen anything


def test_a_reconciliation_committed_between_the_receipt_reads_is_not_misread(
    line_8_of_10, public_catalog
):
    from apps.inventory.services import adjust_stock_lot_quantity

    line, lot = _legacy_line(line_8_of_10, public_catalog)
    cell = line_8_of_10["cells"][2]

    outcome = _receipt_paused_between_reads(
        lambda: receive_stock_lot(create_stock_lot(line, cell, Decimal("5"))),  # 6 + 5 > 10
        lambda: adjust_stock_lot_quantity(lot, Decimal("-1"), comment="Пересчёт"),
    )

    assert outcome["writer"][0] == "ok", outcome
    assert outcome["receipt"][0] == "error", outcome
    assert "можно принять ещё 4" in str(outcome["receipt"][1]), outcome  # not "unproven"
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("5")
    assert remaining_qty(line) == Decimal("4")


def test_a_transfer_during_a_receipt_neither_deadlocks_nor_takes_capacity(line_8_of_10):
    from apps.inventory.lot_provenance import TRANSFER_DERIVED, line_provenance
    from apps.inventory.services import perform_stock_transfer

    line, cells = line_8_of_10["line"], line_8_of_10["cells"]

    outcome = _receipt_paused_between_reads(
        lambda: receive_stock_lot(create_stock_lot(line, cells[2], Decimal("2"))),  # 8 + 2
        lambda: perform_stock_transfer(
            part=line.part_type, from_location=cells[0], to_location=cells[1],
            quantity="3", stock_state=StockLot.Status.AVAILABLE, token="pg-cap-transfer",
        ),
    )

    assert outcome["writer"][0] == "ok", outcome
    assert outcome["receipt"][0] == "ok", outcome
    target = StockLot.objects.get(batch_line=line, location=cells[1])
    classes = {row.lot_id: row.provenance for row in line_provenance(line)}
    assert classes[target.pk] == TRANSFER_DERIVED
    assert remaining_qty(line) == Decimal("0")
    with pytest.raises(InventoryError, match="можно принять ещё 0"):
        create_stock_lot(line, cells[1], Decimal("1"))
