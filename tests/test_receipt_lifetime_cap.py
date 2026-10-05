"""AUD-01: a batch line can never be received above its quantity over its lifetime.

The receivable capacity is the line's quantity minus everything ever received
from it (pending receiving lots, RECEIVE_LOT movements), not minus what is
still on the shelf. Selling, issuing, writing off, moving, returning or
adjusting stock must not reopen it.
"""
from decimal import Decimal

import pytest

from apps.catalog.models import Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    adjust_stock_lot_quantity,
    change_stock_lot_status,
    create_stock_lot,
    get_or_create_section_recount_lot,
    perform_stock_transfer,
    receive_stock_lot,
    remaining_qty,
    update_stock_lot,
)
from apps.procurement.models import BatchLine
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.services import (
    add_stock_lot_to_sale,
    cancel_sale,
    complete_sale,
    create_sale,
)
from apps.warehouse.models import StorageLocation
from apps.writeoffs.models import WriteOffDocument
from apps.writeoffs.services import (
    add_stock_lot_to_write_off,
    complete_write_off,
    create_write_off,
)
from tests.customs_support import remember_customs
from tests.test_piece_stock_boundary import _finalized_line, stock  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def cap(stock, public_catalog):  # noqa: F811
    cells = [stock["location"], stock["other"]] + [
        StorageLocation.objects.create(
            name=f"Cell {n}", code=f"S09-D02-C0{n}", storage_allowed=True, is_active=True
        )
        for n in range(1, 5)
    ]
    part = public_catalog.part("Ремень", article="CAP-1", price="100")
    grease = public_catalog.part(
        "Смазка", article="CAP-KG", price="900", unit=Unit.objects.get(name="Килограмм")
    )
    remember_customs(part, grease)
    return {**stock, "cells": cells, "part": part, "grease": grease}


def _receive(line, cell, quantity):
    lot = create_stock_lot(line, cell, Decimal(quantity))
    return receive_stock_lot(lot)


def _state(line):
    return (
        sorted(StockLot.objects.filter(batch_line=line).values_list("pk", "quantity", "status")),
        StockMovement.objects.count(),
        BatchLine.objects.filter(pk=line.pk).values_list("quantity", flat=True).get(),
    )


def returned_sale_line(stock_return):
    from apps.sales.models import Sale

    return Sale.objects.get(pk=stock_return.source_id).lines.get()


def _sell(cap, lot, quantity):
    sale = create_sale(customer_name="Клиент", by=cap["admin"])
    add_stock_lot_to_sale(sale, lot, Decimal(quantity), unit_price=Decimal("100"))
    return complete_sale(sale, by=cap["admin"])


# --- A. Consumption never reopens receiving ---------------------------------------------


def test_a_sold_piece_does_not_make_room_for_a_second_receipt(cap):
    line = _finalized_line(cap, cap["part"], "10")
    lot = _receive(line, cap["cells"][0], "10")
    _sell(cap, lot, "2")
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("8")
    assert remaining_qty(line) == Decimal("0")
    before = _state(line)

    with pytest.raises(InventoryError, match="уже принято 10 из 10,"):
        create_stock_lot(line, cap["cells"][1], Decimal("2"))

    assert _state(line) == before


# --- B. Partial receipts up to the line, then nothing more ------------------------------


def test_partial_receipts_add_up_to_the_line_and_stop_there(cap):
    line = _finalized_line(cap, cap["part"], "10")
    for cell, quantity in zip(cap["cells"], ("4", "3", "3"), strict=False):
        _receive(line, cell, quantity)
    assert remaining_qty(line) == Decimal("0")
    before = _state(line)

    with pytest.raises(InventoryError, match="можно принять ещё 0"):
        create_stock_lot(line, cap["cells"][3], Decimal("1"))
    with pytest.raises(InventoryError, match="целым"):  # the domain still applies first
        create_stock_lot(line, cap["cells"][3], Decimal("0.001"))
    assert _state(line) == before


def test_a_pending_receiving_lot_already_holds_its_capacity(cap):
    line = _finalized_line(cap, cap["part"], "10")
    pending = create_stock_lot(line, cap["cells"][0], Decimal("7"))  # not received yet
    with pytest.raises(InventoryError, match="уже принято 7 из 10,"):
        create_stock_lot(line, cap["cells"][1], Decimal("4"))
    with pytest.raises(InventoryError, match="можно принять ещё 10"):
        update_stock_lot(pending, location=cap["cells"][0], quantity=Decimal("11"))
    update_stock_lot(pending, location=cap["cells"][0], quantity=Decimal("10"))
    assert remaining_qty(line) == Decimal("0")


# --- D. Measured parts: fractions valid, the cap still holds ---------------------------


def test_measured_receipts_keep_fractions_under_the_same_cap(cap):
    line = _finalized_line(cap, cap["grease"], "10.5")
    _receive(line, cap["cells"][0], "4.25")
    lot = _receive(line, cap["cells"][1], "6.25")
    _sell(cap, lot, "1.5")
    before = _state(line)

    with pytest.raises(InventoryError, match="можно принять ещё 0"):
        create_stock_lot(line, cap["cells"][2], Decimal("0.001"))
    assert _state(line) == before
    assert remaining_qty(line) == Decimal("0")


# --- F. Every other kind of stock change leaves capacity alone ---------------------------


def test_no_consumption_or_correction_reopens_capacity(cap):
    line = _finalized_line(cap, cap["part"], "10")
    lot = _receive(line, cap["cells"][0], "10")

    sale = _sell(cap, lot, "2")
    assert remaining_qty(line) == 0
    order = create_repair_order(customer_name="Клиент", by=cap["admin"])
    add_stock_lot_to_repair_order(order, lot, Decimal("1"))
    complete_repair_order(order, by=cap["admin"])
    assert remaining_qty(line) == 0
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=cap["admin"])
    add_stock_lot_to_write_off(doc, lot, Decimal("1"))
    complete_write_off(doc, by=cap["admin"])
    assert remaining_qty(line) == 0
    adjust_stock_lot_quantity(lot, Decimal("-1"), comment="Пересчёт")
    assert remaining_qty(line) == 0
    perform_stock_transfer(
        part=cap["part"], from_location=cap["cells"][0], to_location=cap["cells"][1],
        quantity="2", stock_state=StockLot.Status.AVAILABLE, token="cap-transfer",
    )
    assert remaining_qty(line) == 0
    cancel_sale(sale, reason="Ошибка", author="Денис", by=cap["admin"])  # returns 2
    assert remaining_qty(line) == 0
    returned = create_return(source=_sell(cap, lot, "1"), reason="Возврат", by=cap["admin"])
    add_sale_line_return(
        returned, returned_sale_line(returned), Decimal("1"), to_location=cap["cells"][3],
        restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(returned, by=cap["admin"])  # opens a new lot in another cell
    assert StockLot.objects.get(batch_line=line, location=cap["cells"][3]).quantity == 1
    assert remaining_qty(line) == 0
    recount_lot = get_or_create_section_recount_lot(
        line, cap["cells"][4], lot_status=StockLot.Status.AVAILABLE
    )
    adjust_stock_lot_quantity(  # as section recount apply writes it
        recount_lot, Decimal("3"), comment="Найдено при пересчёте",
        document_type="section_recount",
    )
    assert remaining_qty(line) == 0

    before = _state(line)
    with pytest.raises(InventoryError, match="можно принять ещё 0"):
        create_stock_lot(line, cap["cells"][2], Decimal("1"))
    assert _state(line) == before


# --- G. There is no receipt reversal ------------------------------------------------------


def test_a_received_lot_cannot_return_to_receiving(cap):
    """Receiving has no reversal: no lot goes back, none is deleted, so the
    lifetime received figure can only grow. Stock taken out again leaves
    through sale, repair, write-off or adjustment, none of which reopen it."""
    line = _finalized_line(cap, cap["part"], "10")
    lot = _receive(line, cap["cells"][0], "6")
    with pytest.raises(InventoryError):
        change_stock_lot_status(lot, StockLot.Status.RECEIVING)
    assert remaining_qty(line) == Decimal("4")
