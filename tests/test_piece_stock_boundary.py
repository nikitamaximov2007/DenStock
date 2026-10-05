"""Physical stock of a piece part is a whole count at every intake and correction.

Receipts, batch lines, direct lots, adjustments, found stock, transfers,
inventory counts, section recounts and counting sessions refuse 1.5 of a part
that is not oil - explicitly, with no rounding - while oil keeps its liters.
Legacy fractional stock is never rewritten here: the audit finds it, stocktaking
can bring it back to a whole count, and a whole-lot move still works.
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.counting.services import (
    CountingError,
    convert_to_receipt,
    record_scan,
    set_line_quantity,
    start_session,
)
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    add_found_stock,
    adjust_stock_lot_quantity,
    create_stock_lot,
    move_stock_lot,
    perform_stock_transfer,
    post_found_stock_group,
    update_stock_lot,
)
from apps.procurement.forms import BatchLineForm
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import LandedCostError, finalize_cost
from apps.receipts.models import Receipt
from apps.receipts.services import (
    ReceiptError,
    add_line,
    create_receipt,
    post_receipt,
    update_line,
)
from apps.stocktaking.models import SectionRecount
from apps.stocktaking.section_recount import (
    SectionRecountError,
    allocate_section_line,
    apply_section_recount,
    complete_section_cell,
    mark_section_ready,
    record_section_scan,
    set_section_line_quantity,
)
from apps.stocktaking.services import (
    StocktakingError,
    add_stock_lot_count_line,
    complete_inventory_count,
    create_inventory_count,
    update_counted_quantity,
)
from apps.warehouse.models import StorageLocation
from tests.test_section_recount import _start, admin, section_data  # noqa: F401

pytestmark = pytest.mark.django_db

FRACTIONS = ["0.5", "1.5", "2.001"]


@pytest.fixture
def stock(public_catalog):
    piece = public_catalog.part("Фильтр масляный", article="SB-1", price="1500")
    oil = public_catalog.part("Масло 4T", article="SB-OIL", price="4000")
    oil.is_oil = True
    oil.oil_package_volume_l = Decimal("4")
    oil.save(update_fields=["is_oil", "oil_package_volume_l"])
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
        "oil": oil,
        "oil_lot": public_catalog.stock(oil, "20"),
    }


def _ledger():
    return (
        sorted(StockLot.objects.values_list("pk", "quantity", "status")),
        StockMovement.objects.count(),
        Batch.objects.count(),
    )


def _legacy(lot, quantity):
    """A fractional piece balance stored before the rule (direct write)."""
    StockLot.objects.filter(pk=lot.pk).update(quantity=Decimal(quantity))
    lot.refresh_from_db()
    return lot


def _finalized_line(stock, part, quantity):
    batch = Batch.objects.create(supplier=stock["supplier"])
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(quantity), unit_cost_currency=Decimal("1")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, stock["admin"])
    line.refresh_from_db()
    return line


# --- Receipts: add, edit and post ---------------------------------------------------------


@pytest.mark.parametrize("value", FRACTIONS)
def test_a_receipt_refuses_a_fractional_piece_on_add_and_edit(stock, value):
    receipt = create_receipt(supplier=stock["supplier"], by=stock["admin"])
    with pytest.raises(ReceiptError, match="целым"):
        add_line(
            receipt, part_type=stock["piece"], quantity=value, unit_cost_rub="10",
            location=stock["location"],
        )
    line = add_line(
        receipt, part_type=stock["piece"], quantity="2", unit_cost_rub="10",
        location=stock["location"],
    )
    with pytest.raises(ReceiptError, match="целым"):
        update_line(
            line, part_type=stock["piece"], quantity=value, unit_cost_rub="10",
            location=stock["location"],
        )
    line.refresh_from_db()
    assert line.quantity == Decimal("2")


def test_a_legacy_fractional_receipt_draft_posts_nothing(stock):
    receipt = create_receipt(supplier=stock["supplier"], by=stock["admin"])
    add_line(
        receipt, part_type=stock["piece"], quantity="3", unit_cost_rub="10",
        location=stock["location"],
    )
    legacy = add_line(
        receipt, part_type=stock["piece"], quantity="2", unit_cost_rub="10",
        location=stock["other"],
    )
    type(legacy).objects.filter(pk=legacy.pk).update(quantity=Decimal("1.5"))
    before = _ledger()

    with pytest.raises(ReceiptError, match="целым"):
        post_receipt(receipt, by=stock["admin"])

    assert _ledger() == before
    assert Receipt.objects.get(pk=receipt.pk).status == Receipt.Status.DRAFT
    # The draft is not stuck: fixing the line lets it post.
    update_line(
        legacy, part_type=stock["piece"], quantity="2", unit_cost_rub="10",
        location=stock["other"],
    )
    assert post_receipt(receipt, by=stock["admin"]).status == Receipt.Status.POSTED


def test_an_oil_receipt_keeps_its_liters(stock):
    receipt = create_receipt(supplier=stock["supplier"], by=stock["admin"])
    add_line(
        receipt, part_type=stock["oil"], quantity="2.75", unit_cost_rub="100",
        location=stock["other"],
    )
    post_receipt(receipt, by=stock["admin"])
    assert StockLot.objects.get(part_type=stock["oil"], location=stock["other"]).quantity == (
        Decimal("2.75")
    )


def test_the_receipt_page_shows_the_refusal_not_a_server_error(stock, client):
    client.force_login(stock["admin"])
    receipt = create_receipt(supplier=stock["supplier"], by=stock["admin"])
    response = client.post(
        reverse("receipt_add_line", args=[receipt.pk]),
        {
            "part_type": stock["piece"].pk, "quantity": "1.5", "unit_cost_rub": "10",
            "location": stock["location"].pk, "comment": "",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert "целым" in response.content.decode()
    assert not receipt.lines.exists()


# --- Procurement batch lines and direct lots ----------------------------------------------


def test_a_batch_line_form_refuses_a_fractional_piece(stock):
    batch = Batch.objects.create(supplier=stock["supplier"])
    form = BatchLineForm(
        data={"part_type": stock["piece"].pk, "quantity": "1.5", "unit_cost_currency": "10"},
        instance=BatchLine(batch=batch),
    )
    assert not form.is_valid()
    assert "целым" in str(form.errors["quantity"])
    oil_form = BatchLineForm(
        data={"part_type": stock["oil"].pk, "quantity": "1.5", "unit_cost_currency": "10"},
        instance=BatchLine(batch=batch),
    )
    assert oil_form.is_valid(), oil_form.errors


def test_a_batch_with_a_fractional_piece_line_is_not_costed(stock):
    batch = Batch.objects.create(supplier=stock["supplier"])
    BatchLine.objects.create(
        batch=batch, part_type=stock["piece"], quantity=Decimal("2.5"),
        unit_cost_currency=Decimal("1"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])

    with pytest.raises(LandedCostError, match="целым"):
        finalize_cost(batch, stock["admin"])

    batch.refresh_from_db()
    assert not batch.cost_finalized and batch.status == Batch.Status.ACCEPTED


@pytest.mark.parametrize("value", FRACTIONS)
def test_a_lot_is_never_created_or_edited_with_a_fractional_piece(stock, value):
    line = _finalized_line(stock, stock["piece"], "10")
    with pytest.raises(InventoryError, match="целым"):
        create_stock_lot(line, stock["other"], Decimal(value))
    lot = create_stock_lot(line, stock["other"], Decimal("2"))
    with pytest.raises(InventoryError, match="целым"):
        update_stock_lot(lot, location=stock["other"], quantity=Decimal(value))
    lot.refresh_from_db()
    assert lot.quantity == Decimal("2")
    oil_line = _finalized_line(stock, stock["oil"], "10")
    assert create_stock_lot(oil_line, stock["other"], Decimal(value)).quantity == Decimal(value)


# --- Adjustments and found stock ----------------------------------------------------------


@pytest.mark.parametrize("value", ["0.5", "-0.5", "1.25"])
def test_an_adjustment_never_leaves_a_fractional_piece_balance(stock, value):
    before = _ledger()
    with pytest.raises(InventoryError, match="целым"):
        adjust_stock_lot_quantity(stock["piece_lot"], Decimal(value), comment="Пересчёт")
    assert _ledger() == before
    movement = adjust_stock_lot_quantity(stock["oil_lot"], Decimal(value), comment="Долив")
    assert movement.quantity == abs(Decimal(value))


def test_the_lot_adjust_page_shows_the_refusal_not_a_server_error(stock, client):
    client.force_login(stock["admin"])
    response = client.post(
        reverse("lot_adjust", args=[stock["piece_lot"].pk]),
        {"delta": "0.5", "comment": "Пересчёт"},
        follow=True,
    )
    assert response.status_code == 200
    assert "целым" in response.content.decode()
    assert StockLot.objects.get(pk=stock["piece_lot"].pk).quantity == Decimal("10")


def test_found_stock_is_counted_in_whole_pieces(stock):
    before = _ledger()
    with pytest.raises(InventoryError, match="целым"):
        add_found_stock(stock["piece"], stock["location"], Decimal("1.5"), by=stock["admin"])
    with pytest.raises(InventoryError, match="целым"):
        post_found_stock_group(
            entries=[{"quantity": "1.5"}], location=stock["location"], token="found-fraction",
        )
    assert _ledger() == before
    lot, _movement = add_found_stock(stock["piece"], stock["location"], by=stock["admin"])
    assert lot.quantity == Decimal("11")


# --- Transfers ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", FRACTIONS)
def test_a_transfer_moves_whole_pieces_and_liters_of_oil(stock, value):
    before = _ledger()
    with pytest.raises(InventoryError, match="целым"):
        perform_stock_transfer(
            part=stock["piece"], from_location=stock["location"], to_location=stock["other"],
            quantity=value, stock_state=StockLot.Status.AVAILABLE, token=f"t-piece-{value}",
            by=stock["admin"],
        )
    assert _ledger() == before
    transfer, created = perform_stock_transfer(
        part=stock["oil"], from_location=stock["location"], to_location=stock["other"],
        quantity=value, stock_state=StockLot.Status.AVAILABLE, token=f"t-oil-{value}",
        by=stock["admin"],
    )
    assert created and transfer.quantity == Decimal(value)


# --- Inventory count, section recount, counting session -----------------------------------


@pytest.mark.parametrize("value", FRACTIONS)
def test_an_inventory_count_takes_whole_pieces_only(stock, value):
    doc = create_inventory_count(by=stock["admin"])
    line = add_stock_lot_count_line(doc, stock["piece_lot"])
    with pytest.raises(StocktakingError, match="целым"):
        update_counted_quantity(line, value)
    line.refresh_from_db()
    assert line.counted_quantity is None
    oil_line = add_stock_lot_count_line(doc, stock["oil_lot"])
    assert update_counted_quantity(oil_line, value).counted_quantity == Decimal(value)


def test_a_section_recount_takes_whole_pieces_only(section_data):  # noqa: F811
    doc = _start(section_data)
    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=section_data["admin"])
    line = doc.lines.get()
    with pytest.raises(SectionRecountError, match="целым"):
        set_section_line_quantity(line, "1,5")
    with pytest.raises(SectionRecountError, match="целым"):
        allocate_section_line(
            line, batch_line_id=section_data["batch_line"].pk, quantity="0.5",
            lot_status=StockLot.Status.AVAILABLE,
        )
    line.refresh_from_db()
    assert line.quantity == Decimal("1")
    assert set_section_line_quantity(line, "3").quantity == Decimal("3")


def test_a_section_recount_apply_reconciles_a_legacy_lot_to_whole(section_data):  # noqa: F811
    lot = section_data["lot"]
    StockLot.objects.filter(pk=lot.pk).update(quantity=Decimal("4.5"))  # legacy
    doc = _start(section_data)
    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=section_data["admin"])
    for number in range(1, 11):
        complete_section_cell(doc, cell_number=number)
    applied = apply_section_recount(mark_section_ready(doc), by=section_data["admin"])

    assert applied.status == SectionRecount.Status.COMPLETED
    quantities = StockLot.objects.filter(batch_line=section_data["batch_line"]).values_list(
        "quantity", flat=True
    )
    assert sorted(quantities) == [Decimal("0"), Decimal("1")]  # every lot whole again


def test_a_counting_session_takes_whole_pieces_before_any_receipt_exists(stock):
    session = start_session(location=stock["other"], by=stock["admin"])
    record_scan(session, "SB-1", by=stock["admin"])
    line = session.lines.get()
    with pytest.raises(CountingError, match="целым"):
        set_line_quantity(line, "1.5")
    line.refresh_from_db()
    assert line.quantity_counted == Decimal("1")

    type(line).objects.filter(pk=line.pk).update(quantity_counted=Decimal("1.5"))  # legacy
    receipts = Receipt.objects.count()
    with pytest.raises(CountingError, match="целым"):
        convert_to_receipt(session, by=stock["admin"])
    assert Receipt.objects.count() == receipts


# --- Legacy fractional stock: found, reconciled, never stuck ------------------------------


def test_legacy_fractional_stock_is_found_and_reconciled_by_a_count(stock):
    lot = _legacy(stock["piece_lot"], "1.5")
    out = StringIO()
    call_command("audit_piece_quantities", stdout=out)
    assert "Остатки лотов: 1" in out.getvalue()

    # Adding a found piece on top would keep it fractional: refused, explained.
    with pytest.raises(InventoryError, match="целым"):
        add_found_stock(stock["piece"], stock["location"], by=stock["admin"])

    doc = create_inventory_count(by=stock["admin"])
    line = add_stock_lot_count_line(doc, lot)
    update_counted_quantity(line, "1")
    complete_inventory_count(doc, by=stock["admin"])

    lot.refresh_from_db()
    assert lot.quantity == Decimal("1")
    movement = StockMovement.objects.filter(stock_lot=lot).latest("pk")
    assert movement.quantity == Decimal("0.5")  # the exact legacy difference, recorded
    out = StringIO()
    call_command("audit_piece_quantities", stdout=out)
    assert "Остатки лотов: 0" in out.getvalue()


def test_a_legacy_fractional_lot_can_still_move_whole_but_not_split(stock):
    legacy = _legacy(stock["piece_lot"], "0.5")
    line = _finalized_line(stock, stock["piece"], "10")
    create_stock_lot(line, stock["location"], Decimal("10"))
    StockLot.objects.filter(batch_line=line).update(status=StockLot.Status.AVAILABLE)
    before = _ledger()

    with pytest.raises(InventoryError, match="дробным остатком"):
        perform_stock_transfer(
            part=stock["piece"], from_location=stock["location"], to_location=stock["other"],
            quantity="2", stock_state=StockLot.Status.AVAILABLE, token="t-legacy-split",
            by=stock["admin"],
        )
    assert _ledger() == before

    moved = move_stock_lot(legacy, stock["other"], by=stock["admin"])
    assert moved.location == stock["other"] and moved.quantity == Decimal("0.5")
