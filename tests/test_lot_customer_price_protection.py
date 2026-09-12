"""Customer defaults follow the actual received item or lot, never cost."""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.actions.cart import cart_rows, open_cart, set_row_quantity
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.pricing import resolve_effective_inventory_customer_price
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    create_repair_order,
    set_repair_line_customer_price,
)
from apps.reports.below_cost_audit import audit_below_cost_customer_documents
from apps.sales.models import Sale, SaleLine
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
    create_sale_from_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.fixture
def price_scene(db, django_user_model):
    user = django_user_model.objects.create_superuser("lot-price", "x@example.test", "pass")
    supplier = Supplier.objects.create(name="Поставка")
    category = Category.objects.create(name="Защита цены")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S09-D01-C01", storage_allowed=True
    )
    part = PartType.objects.create(
        name="421000667",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("1000"),
    )

    def received_lot(*, quantity, snapshot, cost="100"):
        batch = Batch.objects.create(supplier=supplier)
        line = BatchLine.objects.create(
            batch=batch,
            part_type=part,
            quantity=Decimal(quantity),
            unit_cost_currency=Decimal(cost),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, user)
        line.refresh_from_db()
        lot = create_stock_lot(
            line,
            location,
            Decimal(quantity),
            receipt_customer_price_snapshot_rub=Decimal(snapshot),
        )
        receive_stock_lot(lot, by=user)
        return lot

    return user, part, location, received_lot


def test_resolver_uses_only_current_canonical_and_source_snapshot(price_scene):
    _user, part, _location, received_lot = price_scene
    lot = received_lot(quantity="1", snapshot="2500", cost="99999")

    assert resolve_effective_inventory_customer_price(
        lot, part.recommended_price
    ) == Decimal("2500")
    assert resolve_effective_inventory_customer_price(lot, Decimal("3000")) == Decimal("3000")


def test_cart_splits_fifo_lots_with_each_lots_protected_default(price_scene):
    user, part, location, received_lot = price_scene
    first = received_lot(quantity="2", snapshot="1500")
    second = received_lot(quantity="2", snapshot="2500")
    part.recommended_price = Decimal("1000")
    part.save(update_fields=["recommended_price"])

    cart = open_cart("sale", by=user)
    set_row_quantity(cart, part, location, Decimal("4"), by=user)

    prices = list(cart.lines.order_by("stock_lot_id").values_list("stock_lot_id", "unit_price"))
    assert prices == [(first.pk, Decimal("1500.00")), (second.pk, Decimal("2500.00"))]
    # The compact UI never invents one price for several sources. Its known
    # total is the sum of source lines, while the source-level lines stay
    # authoritative for the document.
    row = cart_rows(cart)[0]
    assert row.quantity == Decimal("4")
    assert row.unit_price is None
    assert row.total_price == Decimal("8000")


def test_repair_and_reservation_sale_use_the_selected_source_snapshot(price_scene):
    user, part, _location, received_lot = price_scene
    lot = received_lot(quantity="3", snapshot="2500")
    part.recommended_price = Decimal("1000")
    part.save(update_fields=["recommended_price"])

    repair = create_repair_order(customer_name="Клиент", by=user)
    repair_line = add_stock_lot_to_repair_order(repair, lot, Decimal("1"), by=user)
    assert repair_line.customer_unit_price_rub == Decimal("2500.00")
    set_repair_line_customer_price(repair_line, Decimal("45000"), by=user)
    repair_line.refresh_from_db()
    assert repair_line.customer_unit_price_rub == Decimal("45000.00")

    reservation = create_reservation(customer_name="Клиент", by=user)
    add_stock_lot_to_reservation(reservation, lot, Decimal("1"), by=user)
    activate_reservation(reservation, by=user)
    sale = create_sale_from_reservation(reservation, by=user)
    assert sale.lines.get().unit_price == Decimal("2500.00")


def test_legacy_source_without_snapshot_is_not_backfilled(price_scene):
    _user, part, _location, received_lot = price_scene
    lot = received_lot(quantity="1", snapshot="0")
    lot.receipt_customer_price_snapshot_rub = None
    lot.save(update_fields=["receipt_customer_price_snapshot_rub"])
    part.recommended_price = Decimal("1000")
    part.save(update_fields=["recommended_price"])

    assert resolve_effective_inventory_customer_price(
        lot, part.recommended_price
    ) == Decimal("1000")


def test_explicit_zero_receipt_snapshot_remains_a_valid_sale_price(price_scene):
    user, part, location, received_lot = price_scene
    received_lot(quantity="1", snapshot="0")
    part.recommended_price = None
    part.save(update_fields=["recommended_price"])

    cart = open_cart("sale", by=user)
    set_row_quantity(cart, part, location, Decimal("1"), by=user)

    assert cart.lines.get().unit_price == Decimal("0")


def test_below_cost_audit_reports_sales_and_repairs_with_evidence(price_scene):
    user, part, _location, received_lot = price_scene
    protected = received_lot(quantity="1", snapshot="2500", cost="500")
    historical = received_lot(quantity="1", snapshot="0", cost="500")
    historical.receipt_customer_price_snapshot_rub = None
    historical.save(update_fields=["receipt_customer_price_snapshot_rub"])

    sale = Sale.objects.create(
        customer_name="Покупатель",
        status=Sale.Status.COMPLETED,
        sold_at=timezone.now(),
        sold_by=user,
    )
    SaleLine.objects.create(
        sale=sale,
        part_type=part,
        stock_lot=protected,
        batch=protected.batch,
        batch_line=protected.batch_line,
        quantity=Decimal("1"),
        unit_price=Decimal("1000"),
        total_price=Decimal("1000"),
        unit_cost_rub=Decimal("1500"),
        total_cost_rub=Decimal("1500"),
    )
    repair = RepairOrder.objects.create(
        customer_name="Клиент ремонта",
        status=RepairOrder.Status.COMPLETED,
        completed_at=timezone.now(),
        created_by=user,
    )
    RepairIssueLine.objects.create(
        repair_order=repair,
        part_type=part,
        stock_lot=historical,
        batch=historical.batch,
        batch_line=historical.batch_line,
        quantity=Decimal("1"),
        customer_unit_price_rub=Decimal("1000"),
        unit_cost_rub=Decimal("1500"),
        total_cost_rub=Decimal("1500"),
    )

    rows = audit_below_cost_customer_documents()

    assert {(row.document_kind, row.probable_cause) for row in rows} == {
        ("sale", "A. PRICE_LIST_DROP"),
        ("repair", "C. HISTORICAL_NO_SNAPSHOT"),
    }
    sale_row = next(row for row in rows if row.document_kind == "sale")
    assert sale_row.receipt_customer_price_snapshot_rub == Decimal("2500")
    assert sale_row.accounting_line_cost_rub == Decimal("1500")
