"""Narrow, provenance-guarded corrections for historical receipt cost defects."""

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.inventory.models import StockLot
from apps.procurement.models import money
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.returns.models import StockReturn, StockReturnLine
from apps.sales.models import Sale, SaleLine

from .models import ReceiptLine


class HistoricalLotCostRemediationError(Exception):
    """The requested correction is not proven by the supplied receipt lineage."""


@dataclass(frozen=True)
class HistoricalLotCostPlan:
    lot_id: int
    receipt_line_id: int
    old_cost: Decimal
    new_cost: Decimal
    lot_ids: tuple[int, ...]
    repair_line_ids: tuple[int, ...]
    sale_line_ids: tuple[int, ...]
    return_line_ids: tuple[int, ...]

    @property
    def changes(self) -> int:
        return (
            1
            + len(self.lot_ids)
            + len(self.repair_line_ids)
            + len(self.sale_line_ids)
            + len(self.return_line_ids)
        )


def plan_historical_lot_cost_remediation(*, lot_id, receipt_line_id, expected_old_cost, new_cost):
    """Return the exact, receipt-proven historical cost correction scope.

    This deliberately scopes by the selected lot's BatchLine, never by article,
    catalog price, or a broad search for zero-cost inventory.
    """
    lot = StockLot.objects.select_related("batch_line", "part_type").get(pk=lot_id)
    receipt_line = ReceiptLine.objects.select_related("batch_line", "part_type").get(
        pk=receipt_line_id
    )
    old_cost = money(Decimal(expected_old_cost))
    proven_cost = money(Decimal(new_cost))
    if lot.landed_unit_cost_rub != old_cost:
        raise HistoricalLotCostRemediationError(
            "Текущая себестоимость лота не совпадает с expected-old-cost."
        )
    if (
        receipt_line.batch_line_id != lot.batch_line_id
        or receipt_line.part_type_id != lot.part_type_id
    ):
        raise HistoricalLotCostRemediationError(
            "Строка поступления не является источником выбранного лота."
        )
    if receipt_line.unit_cost_rub != proven_cost:
        raise HistoricalLotCostRemediationError(
            "Новая цена должна точно совпадать с ценой строки поступления."
        )
    if lot.batch_line.allocated_overhead_rub != 0:
        raise HistoricalLotCostRemediationError(
            "Для строки с накладными расходами нужна отдельная подтверждённая методика."
        )

    batch_line_id = lot.batch_line_id
    lots = tuple(StockLot.objects.filter(batch_line_id=batch_line_id).values_list("pk", flat=True))
    repairs = tuple(
        RepairIssueLine.objects.filter(batch_line_id=batch_line_id).values_list("pk", flat=True)
    )
    sales = tuple(SaleLine.objects.filter(batch_line_id=batch_line_id).values_list("pk", flat=True))
    returns = tuple(
        StockReturnLine.objects.filter(batch_line_id=batch_line_id).values_list("pk", flat=True)
    )
    return HistoricalLotCostPlan(
        lot_id=lot.pk,
        receipt_line_id=receipt_line.pk,
        old_cost=old_cost,
        new_cost=proven_cost,
        lot_ids=lots,
        repair_line_ids=repairs,
        sale_line_ids=sales,
        return_line_ids=returns,
    )


@transaction.atomic
def apply_historical_lot_cost_remediation(plan: HistoricalLotCostPlan):
    """Apply one proven plan once; a changed value refuses a second mutation."""
    plan = plan_historical_lot_cost_remediation(
        lot_id=plan.lot_id,
        receipt_line_id=plan.receipt_line_id,
        expected_old_cost=plan.old_cost,
        new_cost=plan.new_cost,
    )
    lot = StockLot.objects.select_for_update().select_related("batch_line").get(pk=plan.lot_id)
    batch_line = lot.batch_line
    batch_line.landed_unit_cost_rub = plan.new_cost
    batch_line.landed_total_cost_rub = money(batch_line.quantity * plan.new_cost)
    batch_line.save(update_fields=["landed_unit_cost_rub", "landed_total_cost_rub", "updated_at"])

    StockLot.objects.filter(pk__in=plan.lot_ids).update(landed_unit_cost_rub=plan.new_cost)
    cost_line_groups = (
        (RepairIssueLine, plan.repair_line_ids),
        (SaleLine, plan.sale_line_ids),
        (StockReturnLine, plan.return_line_ids),
    )
    for model, ids in cost_line_groups:
        for line in model.objects.select_for_update().filter(pk__in=ids):
            line.unit_cost_rub = plan.new_cost
            line.total_cost_rub = money(plan.new_cost * line.quantity)
            update_fields = ["unit_cost_rub", "total_cost_rub"]
            if isinstance(line, SaleLine):
                line.profit_rub = money(line.total_price - line.total_cost_rub)
                update_fields.append("profit_rub")
            line.save(update_fields=update_fields)

    _refresh_headers(RepairOrder, plan.repair_line_ids, "lines", "cost_total")
    _refresh_headers(StockReturn, plan.return_line_ids, "lines", "cost_total")
    sale_ids = SaleLine.objects.filter(pk__in=plan.sale_line_ids).values_list("sale_id", flat=True)
    for sale in Sale.objects.select_for_update().filter(pk__in=sale_ids):
        totals = sale.lines.aggregate(cost=Sum("total_cost_rub"), profit=Sum("profit_rub"))
        sale.cost_total = totals["cost"] or Decimal("0")
        sale.profit_total = totals["profit"] or Decimal("0")
        sale.save(update_fields=["cost_total", "profit_total", "updated_at"])
    return plan


def _refresh_headers(model, line_ids, related_name, field):
    # The explicit branches below avoid a generic relation traversal and keep
    # this remediation's mutation surface auditable.
    if model is RepairOrder:
        document_ids = RepairIssueLine.objects.filter(pk__in=line_ids).values_list(
            "repair_order_id", flat=True
        )
    else:
        document_ids = StockReturnLine.objects.filter(pk__in=line_ids).values_list(
            "stock_return_id", flat=True
        )
    for document in model.objects.select_for_update().filter(pk__in=document_ids):
        total = getattr(document, related_name).aggregate(value=Sum("total_cost_rub"))["value"]
        setattr(document, field, total or Decimal("0"))
        document.save(update_fields=[field, "updated_at"])
