from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.receipts.models import Receipt, ReceiptLine
from apps.receipts.remediation import (
    HistoricalLotCostRemediationError,
    apply_historical_lot_cost_remediation,
    plan_historical_lot_cost_remediation,
)
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.fixture
def bad_lot(db, django_user_model):
    user = django_user_model.objects.create_superuser("cost-fix", "cost@example.test", "pass")
    supplier = Supplier.objects.create(name="Поставщик")
    category = Category.objects.create(name="Себестоимость")
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    part = PartType.objects.create(
        name="Насос",
        category=category,
        unit=unit,
        recommended_price=5000,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    location = StorageLocation.objects.create(name="A", code="A-01", storage_allowed=True)
    receipt = Receipt.objects.create(supplier=supplier)
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("6"), unit_cost_currency=160
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, user)
    ReceiptLine.objects.create(
        receipt=receipt,
        part_type=part,
        quantity=Decimal("6"),
        unit_cost_rub=160,
        location=location,
        batch_line=line,
    )
    line.refresh_from_db()
    line.landed_unit_cost_rub = Decimal("0")
    line.landed_total_cost_rub = Decimal("0")
    line.save(update_fields=["landed_unit_cost_rub", "landed_total_cost_rub"])
    lot = create_stock_lot(line, location, Decimal("6"))
    lot.landed_unit_cost_rub = Decimal("0")
    lot.save(update_fields=["landed_unit_cost_rub"])
    receive_stock_lot(lot, by=user)
    orders = []
    for quantity in ("2", "4"):
        order = create_repair_order(customer_name="Клиент", by=user)
        add_stock_lot_to_repair_order(order, lot, Decimal(quantity), by=user)
        complete_repair_order(order, by=user)
        orders.append(order)
    return user, receipt.lines.get(), lot, orders


def test_plan_is_dry_run_and_apply_repairs_only_proven_lineage(bad_lot):
    _user, receipt_line, lot, orders = bad_lot
    plan = plan_historical_lot_cost_remediation(
        lot_id=lot.pk, receipt_line_id=receipt_line.pk, expected_old_cost="0", new_cost="160"
    )
    lot.refresh_from_db()
    assert lot.landed_unit_cost_rub == Decimal("0")
    assert len(plan.repair_line_ids) == 2
    apply_historical_lot_cost_remediation(plan)
    lot.refresh_from_db()
    assert lot.landed_unit_cost_rub == Decimal("160")
    assert [order.lines.get().total_cost_rub for order in orders] == [
        Decimal("320.00"),
        Decimal("640.00"),
    ]
    assert all(order.lines.get().customer_unit_price_rub == Decimal("5000") for order in orders)
    with pytest.raises(HistoricalLotCostRemediationError):
        apply_historical_lot_cost_remediation(plan)


def test_command_dry_run_does_not_write_and_wrong_guard_refuses(bad_lot):
    _user, receipt_line, lot, _orders = bad_lot
    call_command(
        "repair_historical_lot_cost",
        "--lot-id",
        lot.pk,
        "--receipt-line-id",
        receipt_line.pk,
        "--expected-old-cost",
        "0",
        "--new-cost",
        "160",
        "--source-reference",
        "TEST",
        stdout=StringIO(),
    )
    lot.refresh_from_db()
    assert lot.landed_unit_cost_rub == Decimal("0")
    with pytest.raises(CommandError):
        call_command(
            "repair_historical_lot_cost",
            "--lot-id",
            lot.pk,
            "--receipt-line-id",
            receipt_line.pk,
            "--expected-old-cost",
            "1",
            "--new-cost",
            "160",
            "--source-reference",
            "TEST",
        )
