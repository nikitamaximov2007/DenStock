"""Quantity domains (PIECE / MEASURED) and every physical stock writer.

PIECE parts count whole things; MEASURED parts (oil, and parts counted in
л, кг or м) keep 0.001 precision. No writer may leave a PIECE lot fractional:
a reconciliation may correct a legacy fraction to a whole count, and only the
exact reversal of a recorded fractional line (provenance) may restore one.
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib import admin as django_admin
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory
from django.urls import reverse

from apps.actions.services import ActionError, perform_action
from apps.catalog.models import Unit
from apps.catalog.quantity_units import (
    QuantityDomain,
    quantity_domain,
    unit_quantity_domain,
    validate_part_quantity,
)
from apps.inventory.admin import PartItemAdmin, StockLotAdmin
from apps.inventory.models import PartItem, StockLot
from apps.inventory.services import (
    InventoryError,
    adjust_stock_lot_quantity,
    create_stock_lot,
    perform_stock_transfer,
    return_stock_lot_quantity,
)
from apps.procurement.admin import BatchLineInline
from apps.procurement.models import Batch
from apps.receipts.models import Receipt
from apps.receipts.services import ReceiptError, add_line, create_receipt, post_receipt
from apps.repairs.admin import RepairIssueLineInline
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    RepairError,
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.admin import StockReturnLineInline
from apps.returns.models import StockReturn
from apps.returns.services import cancel_return
from apps.sales.admin import ReservationLineInline, SaleLineInline
from apps.sales.models import Reservation, ReservationLine, Sale, SaleLine
from apps.sales.services import (
    ReservationError,
    SaleError,
    activate_reservation,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    cancel_sale,
    cancel_sale_line_quantity,
    complete_sale,
    create_reservation,
    create_sale,
)
from apps.stocktaking.admin import InventoryCountLineInline
from apps.writeoffs.admin import WriteOffLineInline
from apps.writeoffs.models import WriteOffDocument, WriteOffLine
from apps.writeoffs.services import (
    WriteOffError,
    add_stock_lot_to_write_off,
    cancel_write_off,
    complete_write_off,
    create_write_off,
    quick_write_off,
)
from tests.customer_account_support import make_customer, make_sale
from tests.customs_support import remember_customs
from tests.test_piece_quantity_invariant import _create_request
from tests.test_piece_stock_boundary import _finalized_line, _ledger, _legacy, stock  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def parts(stock, public_catalog):  # noqa: F811
    kilogram = Unit.objects.get(name="Килограмм")
    grease = public_catalog.part("Смазка", article="MS-1", price="900", unit=kilogram)
    remember_customs(stock["piece"], stock["oil"], grease)
    return {**stock, "measured": grease, "measured_lot": public_catalog.stock(grease, "5")}


def _sold(parts, quantity="2"):
    sale = create_sale(customer_name="Клиент", by=parts["admin"])
    add_stock_lot_to_sale(sale, parts["piece_lot"], Decimal(quantity), unit_price=Decimal("100"))
    return complete_sale(sale, by=parts["admin"])


def _lot_quantity(lot):
    return StockLot.objects.get(pk=lot.pk).quantity


# --- A. The domain ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit_name", "domain"),
    [
        ("Штука", QuantityDomain.PIECE),
        ("Комплект", QuantityDomain.PIECE),
        ("Упаковка", QuantityDomain.PIECE),
        ("Литр", QuantityDomain.MEASURED),
        ("Килограмм", QuantityDomain.MEASURED),
        ("Метр", QuantityDomain.MEASURED),
    ],
)
def test_the_seeded_units_split_into_pieces_and_measures(unit_name, domain):
    assert unit_quantity_domain(Unit.objects.get(name=unit_name)) == domain


def test_oil_is_measured_and_an_unclassified_unit_counts_pieces(parts):
    assert quantity_domain(parts["oil"]) == QuantityDomain.MEASURED
    assert quantity_domain(parts["measured"]) == QuantityDomain.MEASURED
    assert quantity_domain(parts["piece"]) == QuantityDomain.PIECE
    gram = Unit.objects.create(name="Грамм", short_name="г")
    assert unit_quantity_domain(gram) == QuantityDomain.PIECE  # until classified in code
    assert validate_part_quantity(Decimal("1.5"), parts["measured"]) is None
    assert validate_part_quantity(Decimal("1.5"), parts["piece"]) is not None


def test_a_measured_non_oil_part_keeps_its_fractions_end_to_end(parts):
    receipt = create_receipt(supplier=parts["supplier"], by=parts["admin"])
    add_line(
        receipt, part_type=parts["measured"], quantity="2.5", unit_cost_rub="10",
        location=parts["other"],
    )
    post_receipt(receipt, by=parts["admin"])
    sale = create_sale(customer_name="Клиент", by=parts["admin"])
    add_stock_lot_to_sale(sale, parts["measured_lot"], Decimal("1.25"), unit_price=Decimal("9"))
    complete_sale(sale, by=parts["admin"])
    adjust_stock_lot_quantity(parts["measured_lot"], Decimal("0.05"), comment="Довес")
    request, _ = _create_request(parts["measured"], "0.5", "measured-request".ljust(40, "k"))

    assert _lot_quantity(parts["measured_lot"]) == Decimal("3.8")
    assert request.lines.get().quantity_requested == Decimal("0.5")


def test_a_part_with_history_keeps_its_quantity_domain(parts):
    piece = parts["piece"]
    piece.unit = Unit.objects.get(name="Литр")
    with pytest.raises(ValidationError, match="штучной и измеряемой"):
        piece.full_clean()
    fresh = parts["piece"].__class__.objects.create(
        name="Новая", category=piece.category, unit=Unit.objects.get(name="Штука"),
        tracking_mode=piece.tracking_mode,
    )
    fresh.unit = Unit.objects.get(name="Литр")
    fresh.full_clean()  # no stock, no history: free to classify


def test_a_unit_in_use_cannot_be_renamed_across_domains(parts):
    kilogram = Unit.objects.get(name="Килограмм")
    kilogram.name, kilogram.short_name = "Набор", "наб"
    with pytest.raises(ValidationError, match="штучной и измеряемой"):
        kilogram.full_clean()
    unused = Unit.objects.get(name="Метр")
    unused.name, unused.short_name = "Пара", "пар"
    unused.full_clean()


# --- B. Consumption from a legacy fractional lot -------------------------------------------


def test_sale_repair_and_write_off_completion_refuse_a_legacy_lot_with_their_own_error(parts):
    _legacy(parts["piece_lot"], "9.5")
    before = _ledger()
    sale = create_sale(customer_name="Клиент", by=parts["admin"])
    add_stock_lot_to_sale(sale, parts["piece_lot"], Decimal("1"), unit_price=Decimal("100"))
    order = create_repair_order(customer_name="Клиент", by=parts["admin"])
    add_stock_lot_to_repair_order(order, parts["piece_lot"], Decimal("1"))
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=parts["admin"])
    add_stock_lot_to_write_off(doc, parts["piece_lot"], Decimal("1"))

    with pytest.raises(SaleError, match="дробным остатком"):
        complete_sale(sale, by=parts["admin"])
    with pytest.raises(RepairError, match="дробным остатком"):
        complete_repair_order(order, by=parts["admin"])
    with pytest.raises(WriteOffError, match="дробным остатком"):
        complete_write_off(doc, by=parts["admin"])
    with pytest.raises(WriteOffError, match="дробным остатком"):
        quick_write_off(
            part=parts["piece"], scanned_code="SB-1", reason="Брак", business_author="Денис",
            quantity="1", location_id=parts["location"].pk, by=parts["admin"],
        )
    with pytest.raises(ActionError, match="дробным остатком"):
        perform_action(
            part=parts["piece"], location=parts["location"], action_type="sale",
            quantity="1", customer_comment="Клиент", by=parts["admin"],
        )

    assert _ledger() == before
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.DRAFT
    assert RepairOrder.objects.get(pk=order.pk).status == RepairOrder.Status.DRAFT


def test_the_sale_page_shows_the_legacy_lot_refusal_not_a_server_error(parts, client):
    _legacy(parts["piece_lot"], "9.5")
    client.force_login(parts["admin"])
    sale = create_sale(customer_name="Клиент", by=parts["admin"])
    add_stock_lot_to_sale(sale, parts["piece_lot"], Decimal("1"), unit_price=Decimal("100"))
    response = client.post(reverse("sale_complete", args=[sale.pk]), follow=True)
    assert response.status_code == 200
    assert "дробным остатком" in response.content.decode()


def test_a_reservation_with_a_legacy_fractional_line_is_not_activated(parts):
    reservation = create_reservation(customer_name="Клиент", by=parts["admin"])
    add_stock_lot_to_reservation(reservation, parts["piece_lot"], Decimal("2"))
    ReservationLine.objects.filter(reservation=reservation).update(quantity=Decimal("1.5"))
    with pytest.raises(ReservationError, match="целым"):
        activate_reservation(reservation, by=parts["admin"])
    assert Reservation.objects.get(pk=reservation.pk).status == Reservation.Status.DRAFT


# --- C. Transfers: split and merge -----------------------------------------------------------


def test_a_transfer_never_merges_into_a_legacy_fractional_lot(parts, public_catalog):
    part = public_catalog.part("Втулка", article="TR-1", price="10")
    line = _finalized_line(parts, part, "20")
    source = create_stock_lot(line, parts["location"], Decimal("10"))
    target = create_stock_lot(line, parts["other"], Decimal("1"))
    StockLot.objects.filter(pk__in=[source.pk, target.pk]).update(
        status=StockLot.Status.AVAILABLE
    )
    _legacy(target, "0.5")
    before = _ledger()

    with pytest.raises(InventoryError, match="целым"):
        perform_stock_transfer(
            part=part, from_location=parts["location"], to_location=parts["other"],
            quantity="1", stock_state=StockLot.Status.AVAILABLE, token="merge-legacy",
        )
    assert _ledger() == before


# --- D. Compensation needs provenance --------------------------------------------------------


def test_cancelling_a_legacy_fractional_sale_restores_exactly_what_it_recorded(parts):
    sale = _sold(parts)  # lot 10 -> 8
    SaleLine.objects.filter(sale=sale).update(quantity=Decimal("1.5"))  # legacy sale of 1.5
    cancel_sale(sale, reason="Ошибка", author="Денис", by=parts["admin"])
    assert _lot_quantity(parts["piece_lot"]) == Decimal("9.5")  # the recorded state


def test_a_legacy_return_and_its_cancellation_are_exact_compensations(parts):
    sale = _sold(parts)
    line = sale.lines.get()
    SaleLine.objects.filter(pk=line.pk).update(quantity=Decimal("1.5"))
    document = cancel_sale_line_quantity(line, "1.5", reason="Ошибка", author="Денис")
    assert _lot_quantity(parts["piece_lot"]) == Decimal("9.5")
    cancel_return(document, by=parts["admin"], reason="Ошибка отмены")
    assert _lot_quantity(parts["piece_lot"]) == Decimal("8")
    assert StockReturn.objects.get(pk=document.pk).status == StockReturn.Status.CANCELED


def test_cancelling_a_legacy_fractional_write_off_restores_it_exactly(parts):
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=parts["admin"])
    add_stock_lot_to_write_off(doc, parts["piece_lot"], Decimal("2"))
    complete_write_off(doc, by=parts["admin"])  # lot 10 -> 8
    WriteOffLine.objects.filter(write_off=doc).update(quantity=Decimal("1.5"))
    cancel_write_off(doc, by=parts["admin"])
    assert _lot_quantity(parts["piece_lot"]) == Decimal("9.5")


def test_a_fraction_without_provenance_is_never_restored(parts):
    lot = parts["piece_lot"]
    whole_line = _sold(parts).lines.get()
    other_part_line = make_sale(
        make_customer("Иван"), parts["measured"], lot=parts["measured_lot"], quantity="1.5"
    ).lines.get()

    for compensates in (None, whole_line, other_part_line):
        with pytest.raises(InventoryError, match="целым"):
            return_stock_lot_quantity(
                lot.batch_line, lot.location, Decimal("0.5"), unit_cost_rub=Decimal("1"),
                restock_status=StockLot.Status.AVAILABLE, stock_lot=lot,
                compensates=compensates,
            )
    assert _lot_quantity(lot) == Decimal("8")


# --- E. Error mapping ---------------------------------------------------------------------------


def test_a_stock_error_during_receipt_posting_is_a_receipt_error(parts, monkeypatch):
    receipt = create_receipt(supplier=parts["supplier"], by=parts["admin"])
    add_line(
        receipt, part_type=parts["piece"], quantity="2", unit_cost_rub="10",
        location=parts["other"],
    )

    def refused(*args, **kwargs):
        raise InventoryError("Ячейка заблокирована пересчётом.")

    monkeypatch.setattr("apps.receipts.services.create_stock_lot", refused)
    before = _ledger()
    with pytest.raises(ReceiptError, match="заблокирована"):
        post_receipt(receipt, by=parts["admin"])
    assert _ledger() == before
    assert Receipt.objects.get(pk=receipt.pk).status == Receipt.Status.DRAFT
    assert Batch.objects.count() == before[2]


# --- F. Admin ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inline", "parent"),
    [
        (SaleLineInline, Sale),
        (ReservationLineInline, Reservation),
        (RepairIssueLineInline, RepairOrder),
        (StockReturnLineInline, StockReturn),
        (WriteOffLineInline, WriteOffDocument),
        (InventoryCountLineInline, None),
        (BatchLineInline, Batch),
    ],
)
def test_admin_document_lines_are_read_only(inline, parent, parts):
    request = RequestFactory().get("/")
    request.user = parts["admin"]
    instance = inline(parent or Sale, django_admin.site)
    assert not instance.has_add_permission(request, None)
    assert not instance.has_change_permission(request, None)
    assert not instance.has_delete_permission(request, None)


def test_admin_cannot_reassign_a_lot_or_item_to_another_part(parts, client):
    request = RequestFactory().get("/")
    request.user = parts["admin"]
    for model_admin, model in ((StockLotAdmin, StockLot), (PartItemAdmin, PartItem)):
        readonly = model_admin(model, django_admin.site).get_readonly_fields(request)
        assert {"part_type", "batch_line", "quantity" if model is StockLot else "status"} <= set(
            readonly
        )
    client.force_login(parts["admin"])
    page = client.get(
        reverse("admin:inventory_stocklot_change", args=[parts["oil_lot"].pk])
    ).content.decode()
    assert 'name="part_type"' not in page and 'name="quantity"' not in page


def test_admin_sale_page_offers_no_line_quantity_input(parts, client):
    sale = _sold(parts)
    client.force_login(parts["admin"])
    page = client.get(reverse("admin:sales_sale_change", args=[sale.pk])).content.decode()
    assert "lines-0-quantity" not in page


# --- G. Audit ------------------------------------------------------------------------------------


def test_the_audit_reports_piece_fractions_but_not_measured_ones(parts):
    make_sale(make_customer("Анна"), parts["measured"], lot=parts["measured_lot"], quantity="1.5")
    _legacy(parts["piece_lot"], "9.5")
    out = StringIO()
    call_command("audit_piece_quantities", stdout=out)
    text = out.getvalue()
    assert "Остатки лотов: 1" in text
    assert "Строки продаж: 0" in text
    assert "Измеряемые детали (не масло) по единицам:\n  кг: 1" in text
