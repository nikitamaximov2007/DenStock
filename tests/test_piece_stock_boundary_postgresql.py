"""PostgreSQL 16: a refused piece fraction leaves no stock effect, even under races.

Each refusal must happen inside the operation's own transaction, before any
row it would write is committed, and must release its locks so a concurrent
valid operation on the same lot completes normally.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.db import close_old_connections, connection

from apps.inventory.models import FoundStockPosting, StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    adjust_stock_lot_quantity,
    perform_stock_transfer,
    post_found_stock_group,
)
from apps.procurement.models import Batch
from apps.receipts.models import Receipt
from apps.receipts.services import ReceiptError, add_line, create_receipt, post_receipt
from apps.warehouse.models import StorageLocation

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="PostgreSQL 16 transaction qualification"
    ),
]


@pytest.fixture
def stock(public_catalog):
    piece = public_catalog.part("Фильтр масляный", article="PG-1", price="1500")
    other_piece = public_catalog.part("Свеча", article="PG-2", price="300")
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


def _race(*calls):
    barrier = Barrier(len(calls))

    def run(fn):
        close_old_connections()
        try:
            barrier.wait(20)
            return fn(), None
        except Exception as exc:  # the outcome of each side is asserted below
            return None, exc
        finally:
            close_old_connections()

    with ThreadPoolExecutor(len(calls)) as pool:
        return [future.result(30) for future in [pool.submit(run, fn) for fn in calls]]


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


def test_a_found_group_with_one_legacy_lot_rolls_back_every_entry(stock):
    StockLot.objects.filter(pk=stock["other_lot"].pk).update(quantity=Decimal("3.5"))
    before = _ledger()
    entries = [
        {"source": "warehouse", "source_id": stock["piece"].pk, "exact_number": "PG-1",
         "quantity": 1},
        {"source": "warehouse", "source_id": stock["other_piece"].pk, "exact_number": "PG-2",
         "quantity": 1},
    ]

    with pytest.raises(InventoryError, match="целым"):
        post_found_stock_group(
            entries=entries, location=stock["location"], token="pg-found-legacy",
            by=stock["admin"],
        )

    # The first entry's +1 and the idempotency row are gone with the second's refusal.
    assert _ledger() == before


def test_a_refused_fraction_does_not_block_a_concurrent_valid_adjustment(stock):
    lot = stock["piece_lot"]
    results = _race(
        lambda: adjust_stock_lot_quantity(lot, Decimal("0.5"), comment="Дробь"),
        lambda: adjust_stock_lot_quantity(lot, Decimal("1"), comment="Найдено"),
    )

    errors = [exc for _value, exc in results if exc is not None]
    assert len(errors) == 1 and isinstance(errors[0], InventoryError)
    assert "целым" in str(errors[0])
    lot.refresh_from_db()
    assert lot.quantity == Decimal("11")
    assert list(StockMovement.objects.filter(stock_lot=lot, comment__in=["Дробь", "Найдено"])
                .values_list("comment", "quantity")) == [("Найдено", Decimal("1.000"))]


def test_racing_transfers_one_fractional_one_whole_move_only_whole_pieces(stock):
    def transfer(quantity, token):
        return perform_stock_transfer(
            part=stock["piece"], from_location=stock["location"], to_location=stock["other"],
            quantity=quantity, stock_state=StockLot.Status.AVAILABLE, token=token,
            by=stock["admin"],
        )

    results = _race(lambda: transfer("1.5", "pg-t-frac"), lambda: transfer("3", "pg-t-whole"))

    assert isinstance(results[0][1], InventoryError) and results[1][1] is None
    assert StockLot.objects.get(pk=stock["piece_lot"].pk).quantity == Decimal("7")
    assert StockLot.objects.get(part_type=stock["piece"], location=stock["other"]).quantity == (
        Decimal("3")
    )
    quantities = StockLot.objects.filter(part_type=stock["piece"]).values_list(
        "quantity", flat=True
    )
    assert all(value == value.to_integral_value() for value in quantities)
