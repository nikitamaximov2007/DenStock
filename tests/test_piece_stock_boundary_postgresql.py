"""PostgreSQL 16: piece stock stays whole under real lock races and rollbacks.

Every race is forced, not hoped for: the holder locks the lot row, waits until
PostgreSQL reports the contender's backend as blocked on a lock
(pg_stat_activity.wait_event_type = 'Lock'), and only then commits or rolls
back. A test fails if the contender was never seen waiting, so a race that did
not happen cannot pass. Fixtures create the units they need, so nothing depends
on data seeded by migrations surviving a transactional test (they flush it,
and --reuse-db has no serialized copy to restore).
"""
import time
from decimal import Decimal
from threading import Event, Thread

import pytest
from django.db import close_old_connections, connection, transaction

from apps.catalog.models import Unit
from apps.inventory.models import (
    FoundStockPosting,
    NumberSequence,
    StockLot,
    StockMovement,
)
from apps.inventory.services import (
    InventoryError,
    adjust_stock_lot_quantity,
    post_found_stock_group,
)
from apps.procurement.models import Batch
from apps.receipts.models import Receipt
from apps.receipts.services import ReceiptError, add_line, create_receipt, post_receipt
from apps.sales.models import Sale
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.warehouse.models import StorageLocation
from tests.customs_support import remember_customs

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="PostgreSQL 16 transaction qualification"
    ),
]

UNITS = (("Штука", "шт"), ("Литр", "л"), ("Килограмм", "кг"), ("Метр", "м"))
# Document numbering rows seeded by the apps' migrations (key, prefix).
SEQUENCES = (
    ("part_item", "DS-"), ("receipt", "ПОС-"), ("repair_order", "R-"),
    ("stock_return", "RET-"), ("reservation", "РЕЗ-"), ("sale", "S-"),
    ("inventory_count", "IC-"), ("write_off", "WO-"),
)


@pytest.fixture(autouse=True)
def units(db):
    """Reference rows a transactional test may find flushed (e.g. with --reuse-db)."""
    for name, short_name in UNITS:
        Unit.objects.get_or_create(name=name, defaults={"short_name": short_name})
    for key, prefix in SEQUENCES:
        NumberSequence.objects.get_or_create(key=key, defaults={"prefix": prefix})


@pytest.fixture
def stock(units, public_catalog):
    piece = public_catalog.part("Фильтр масляный", article="PG-1", price="1500")
    other_piece = public_catalog.part("Свеча", article="PG-2", price="300")
    remember_customs(piece, other_piece)
    other = StorageLocation.objects.create(
        name="Second cell", code="S09-D01-C02", storage_allowed=True, is_active=True
    )
    return {
        "admin": public_catalog.user,
        "location": public_catalog.location,
        "other": other,
        "supplier": public_catalog.supplier,
        "piece": piece,
        "piece_lot": public_catalog.stock(piece, "10"),
        "other_piece": other_piece,
        "other_lot": public_catalog.stock(other_piece, "4"),
    }


def _ledger():
    return (
        sorted(StockLot.objects.values_list("pk", "quantity", "status")),
        StockMovement.objects.count(),
        Batch.objects.count(),
        FoundStockPosting.objects.count(),
    )


def _backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _waits_on_lock(pid, timeout=20):
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [pid])
            row = cursor.fetchone()
            if row and row[0] == "Lock":
                return True
            time.sleep(0.02)
    return False


class _Rollback(Exception):
    pass


def _forced_race(lot_pk, *, holder, contender, holder_commits=True):
    """Holder locks the lot, waits until the contender blocks on it, then acts."""
    locked, contender_pid = Event(), {}
    outcome = {"holder": None, "contender": None, "contender_waited": False}

    def run_contender():
        close_old_connections()
        try:
            contender_pid["pid"] = _backend_pid()
            locked.wait(20)
            outcome["contender"] = ("ok", contender())
        except Exception as exc:  # each side's outcome is asserted by the test
            outcome["contender"] = ("error", exc)
        finally:
            close_old_connections()

    def run_holder():
        close_old_connections()
        try:
            with transaction.atomic():
                StockLot.objects.select_for_update().get(pk=lot_pk)
                locked.set()
                while "pid" not in contender_pid:
                    time.sleep(0.01)
                outcome["contender_waited"] = _waits_on_lock(contender_pid["pid"])
                try:
                    outcome["holder"] = ("ok", holder())
                except Exception as exc:
                    outcome["holder"] = ("error", exc)
                if not holder_commits or outcome["holder"][0] == "error":
                    raise _Rollback
        except _Rollback:
            pass
        finally:
            close_old_connections()

    threads = [Thread(target=run_contender), Thread(target=run_holder)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert outcome["contender_waited"], "the contender never blocked on the lot lock"
    return outcome


def test_a_fractional_adjustment_waiting_behind_a_valid_one_is_refused_cleanly(stock):
    lot = stock["piece_lot"]
    outcome = _forced_race(
        lot.pk,
        holder=lambda: adjust_stock_lot_quantity(lot, Decimal("1"), comment="Найдено"),
        contender=lambda: adjust_stock_lot_quantity(lot, Decimal("0.5"), comment="Дробь"),
    )

    assert outcome["holder"][0] == "ok"
    assert outcome["contender"][0] == "error"
    assert isinstance(outcome["contender"][1], InventoryError)
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("11")
    assert not StockMovement.objects.filter(stock_lot=lot, comment="Дробь").exists()


def test_two_reconciliations_of_one_legacy_lot_cannot_both_apply(stock):
    lot = stock["piece_lot"]
    StockLot.objects.filter(pk=lot.pk).update(quantity=Decimal("1.5"))
    outcome = _forced_race(
        lot.pk,
        holder=lambda: adjust_stock_lot_quantity(lot, Decimal("-0.5"), comment="Пересчёт A"),
        contender=lambda: adjust_stock_lot_quantity(lot, Decimal("-0.5"), comment="Пересчёт B"),
    )

    # B computed its correction from the same 1.5, but is judged on the locked,
    # already reconciled balance: 1 - 0.5 would be fractional again.
    assert outcome["holder"][0] == "ok"
    assert outcome["contender"][0] == "error"
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("1")
    assert list(
        StockMovement.objects.filter(stock_lot=lot, comment__startswith="Пересчёт")
        .values_list("comment", "quantity")
    ) == [("Пересчёт A", Decimal("0.500"))]


def test_a_sale_waiting_on_a_reconciliation_sells_from_the_whole_balance(stock):
    lot = stock["piece_lot"]
    StockLot.objects.filter(pk=lot.pk).update(quantity=Decimal("1.5"))
    sale = create_sale(customer_name="Клиент", by=stock["admin"])
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("100"))

    outcome = _forced_race(
        lot.pk,
        holder=lambda: adjust_stock_lot_quantity(lot, Decimal("-0.5"), comment="Пересчёт"),
        contender=lambda: complete_sale(sale, by=stock["admin"]),
    )

    assert outcome["holder"][0] == "ok" and outcome["contender"][0] == "ok"
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("0")


def test_a_refused_group_releases_its_lock_and_leaves_no_partial_write(stock):
    """The holder adds +1 to lot A, then hits a legacy lot and rolls back."""
    StockLot.objects.filter(pk=stock["other_lot"].pk).update(quantity=Decimal("3.5"))
    lot = stock["piece_lot"]
    entries = [
        {"source": "warehouse", "source_id": stock["piece"].pk, "exact_number": "PG-1",
         "quantity": 1},
        {"source": "warehouse", "source_id": stock["other_piece"].pk, "exact_number": "PG-2",
         "quantity": 1},
    ]
    outcome = _forced_race(
        lot.pk,
        holder=lambda: post_found_stock_group(
            entries=entries, location=stock["location"], token="pg-group", by=stock["admin"]
        ),
        contender=lambda: adjust_stock_lot_quantity(lot, Decimal("1"), comment="После отката"),
    )

    assert outcome["holder"][0] == "error"
    assert isinstance(outcome["holder"][1], InventoryError)
    assert outcome["contender"][0] == "ok"
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("11")  # not 12
    assert not FoundStockPosting.objects.exists()


def test_a_receipt_with_one_legacy_fractional_line_commits_nothing(stock):
    receipt = create_receipt(supplier=stock["supplier"], by=stock["admin"])
    add_line(
        receipt, part_type=stock["piece"], quantity="3", unit_cost_rub="10",
        location=stock["other"],
    )
    legacy = add_line(
        receipt, part_type=stock["other_piece"], quantity="2", unit_cost_rub="10",
        location=stock["other"],
    )
    type(legacy).objects.filter(pk=legacy.pk).update(quantity=Decimal("1.5"))
    before = _ledger()

    with pytest.raises(ReceiptError, match="целым"):
        post_receipt(receipt, by=stock["admin"])

    assert _ledger() == before
    assert Receipt.objects.get(pk=receipt.pk).status == Receipt.Status.DRAFT
