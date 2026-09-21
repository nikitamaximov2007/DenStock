"""Fail-closed historical lot price reconstruction from counting evidence."""

from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.catalog.models import Category, PartType, Unit
from apps.counting.models import InventoryCountingLine, InventoryCountingSession
from apps.inventory.historical_price_backfill import (
    apply_historical_price_backfill,
    build_historical_price_backfill_plan,
)
from apps.inventory.models import PartItem, StockLot
from apps.inventory.pricing import resolve_effective_inventory_customer_price
from apps.inventory.services import return_stock_lot_quantity
from apps.receipts.models import Receipt, ReceiptLine
from apps.receipts.services import post_receipt
from apps.sales.models import Sale, SaleLine
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.fixture
def scene(db, django_user_model):
    user = django_user_model.objects.create_superuser("forensic", "f@example.test", "pass")
    supplier = Supplier.objects.create(name="Forensic supplier")
    category = Category.objects.create(name="Forensic category")
    location = StorageLocation.objects.create(
        name="Forensic cell", code="S08-D01-C01", storage_allowed=True
    )
    unit = Unit.objects.get(name="Штука")

    def received(*, price="2500", serial=False):
        part = PartType.objects.create(
            name=f"Part {PartType.objects.count()}", category=category, unit=unit,
            tracking_mode=PartType.TrackingMode.SERIAL if serial else PartType.TrackingMode.BULK,
            recommended_price=Decimal("1000"),
        )
        receipt = Receipt.objects.create(supplier=supplier, created_by=user)
        ReceiptLine.objects.create(
            receipt=receipt, part_type=part, location=location,
            quantity=Decimal("1"), unit_cost_rub=Decimal("100"),
        )
        post_receipt(receipt, by=user)
        if serial:
            item = PartItem.objects.get(part_type=part)
            item.receipt_customer_price_snapshot_rub = None
            item.save(update_fields=["receipt_customer_price_snapshot_rub"])
            return part, receipt, item
        lot = StockLot.objects.get(part_type=part)
        lot.receipt_customer_price_snapshot_rub = None
        lot.save(update_fields=["receipt_customer_price_snapshot_rub"])
        return part, receipt, lot

    def evidence(receipt, part, *, price="2500", normalized="one"):
        session = InventoryCountingSession.objects.create(
            storage_location=location, full_address=location.code,
            status=InventoryCountingSession.Status.POSTED, converted_receipt=receipt,
        )
        line = InventoryCountingLine.objects.create(
            session=session, scanned_value=normalized, normalized_value=normalized,
            warehouse_part=part, source=InventoryCountingLine.Source.WAREHOUSE,
            quantity_counted=Decimal("1"), scan_count=1,
            final_customer_price_rub=None if price is None else Decimal(price),
        )
        return session, line

    return {"user": user, "location": location, "received": received, "evidence": evidence}


def _row_for(plan, lot):
    return next(row for row in plan.rows if row.lot_id == lot.id)


def test_unique_immutable_evidence_backfills_historical_snapshot(scene):
    part, receipt, lot = scene["received"]()
    scene["evidence"](receipt, part, price="2500")

    plan = apply_historical_price_backfill()
    lot.refresh_from_db()

    assert _row_for(plan, lot).outcome == "eligible"
    assert lot.receipt_customer_price_snapshot_rub == Decimal("2500.00")
    assert resolve_effective_inventory_customer_price(lot, Decimal("1000")) == Decimal("1000")


def test_no_evidence_remains_null_and_dry_run_writes_nothing(scene):
    _part, _receipt, lot = scene["received"]()

    plan = build_historical_price_backfill_plan()
    call_command("backfill_counting_receipt_customer_price_snapshots")
    lot.refresh_from_db()

    assert _row_for(plan, lot).reason == "no_posted_counting_session"
    assert lot.receipt_customer_price_snapshot_rub is None


def test_ambiguous_session_fails_closed(scene):
    part, receipt, lot = scene["received"]()
    scene["evidence"](receipt, part, normalized="first")
    scene["evidence"](receipt, part, normalized="second")

    row = _row_for(build_historical_price_backfill_plan(), lot)

    assert (row.outcome, row.reason) == ("skipped", "ambiguous_counting_session")


def test_conflicting_exact_part_evidence_fails_closed(scene):
    part, receipt, lot = scene["received"]()
    session, _line = scene["evidence"](receipt, part, price="2500", normalized="first")
    InventoryCountingLine.objects.create(
        session=session, scanned_value="second", normalized_value="second",
        warehouse_part=part, source=InventoryCountingLine.Source.WAREHOUSE,
        quantity_counted=Decimal("1"), scan_count=1, final_customer_price_rub=Decimal("2600"),
    )

    row = _row_for(build_historical_price_backfill_plan(), lot)

    assert (row.outcome, row.reason) == ("skipped", "conflicting_evidence")


def test_existing_snapshot_is_never_overwritten(scene):
    part, receipt, lot = scene["received"]()
    lot.receipt_customer_price_snapshot_rub = Decimal("1700")
    lot.save(update_fields=["receipt_customer_price_snapshot_rub"])
    scene["evidence"](receipt, part, price="2500")

    row = _row_for(apply_historical_price_backfill(), lot)
    lot.refresh_from_db()

    assert row.reason == "snapshot_exists"
    assert lot.receipt_customer_price_snapshot_rub == Decimal("1700.00")


@pytest.mark.parametrize("price", ["0", "-1", "2500.001"])
def test_invalid_or_nonpositive_evidence_is_rejected(scene, price):
    part, receipt, lot = scene["received"]()
    scene["evidence"](receipt, part, price=price)

    row = _row_for(build_historical_price_backfill_plan(), lot)

    assert row.outcome == "skipped"
    assert row.reason in {"nonpositive_evidence", "unsupported_precision"}


def test_second_apply_is_idempotent(scene):
    part, receipt, lot = scene["received"]()
    scene["evidence"](receipt, part)

    apply_historical_price_backfill()
    second = apply_historical_price_backfill()
    lot.refresh_from_db()

    assert _row_for(second, lot).reason == "snapshot_exists"
    assert lot.receipt_customer_price_snapshot_rub == Decimal("2500.00")


def test_part_item_without_separate_proof_is_untouched(scene):
    _part, _receipt, item = scene["received"](serial=True)

    apply_historical_price_backfill()
    item.refresh_from_db()

    assert item.receipt_customer_price_snapshot_rub is None


def test_return_keeps_reconstructed_snapshot(scene):
    part, receipt, lot = scene["received"]()
    scene["evidence"](receipt, part)
    apply_historical_price_backfill()

    returned = return_stock_lot_quantity(
        lot.batch_line, scene["location"], Decimal("1"), unit_cost_rub=Decimal("100"),
        restock_status=StockLot.Status.AVAILABLE, stock_lot=lot, by=scene["user"],
    )

    assert returned.receipt_customer_price_snapshot_rub == Decimal("2500.00")


def test_historical_sale_rows_are_unchanged(scene):
    part, receipt, lot = scene["received"]()
    scene["evidence"](receipt, part)
    sale = Sale.objects.create(customer_name="Historical", status=Sale.Status.DRAFT)
    line = SaleLine.objects.create(
        sale=sale, part_type=part, stock_lot=lot, batch=lot.batch, batch_line=lot.batch_line,
        quantity=Decimal("1"), unit_price=Decimal("999"), total_price=Decimal("999"),
        unit_cost_rub=Decimal("100"), total_cost_rub=Decimal("100"),
    )
    before = (line.unit_price, line.total_price, line.unit_cost_rub, line.total_cost_rub)

    apply_historical_price_backfill()
    line.refresh_from_db()

    assert (line.unit_price, line.total_price, line.unit_cost_rub, line.total_cost_rub) == before
