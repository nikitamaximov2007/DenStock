"""Read-only forensic audit for completed customer-facing part issues."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from apps.catalog.models import PartNumber
from apps.inventory.pricing import resolve_effective_inventory_customer_price
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.returns.models import StockReturn, StockReturnLine
from apps.sales.models import Sale, SaleLine


@dataclass(frozen=True)
class BelowCostRow:
    document_kind: str
    document_id: int
    document_number: str
    document_date: object
    customer: str
    line_id: int
    article: str
    part_name: str
    quantity: Decimal
    source_kind: str
    source_id: int
    customer_unit_price_rub: Decimal | None
    customer_line_amount_rub: Decimal | None
    accounting_unit_cost_rub: Decimal
    accounting_line_cost_rub: Decimal
    delta_rub: Decimal | None
    margin_percent: Decimal | None
    inventory_history: str
    manual_override_identifiable: str
    receipt_customer_price_snapshot_rub: Decimal | None
    current_canonical_price_rub: Decimal | None
    probable_cause: str
    explanation: str

    def asdict(self):
        return asdict(self)


def audit_below_cost_customer_documents() -> list[BelowCostRow]:
    """Return every completed sale/repair line whose known customer amount is below cost.

    This inspection intentionally reads frozen document values.  It never
    reconciles a price, a cost, a return, or a receipt snapshot.
    """
    sales = list(
        SaleLine.objects.filter(sale__status=Sale.Status.COMPLETED).select_related(
            "sale", "part_type", "part_item", "stock_lot"
        )
    )
    repairs = list(
        RepairIssueLine.objects.filter(repair_order__status=RepairOrder.Status.COMPLETED)
        .select_related("repair_order", "part_type", "part_item", "stock_lot")
    )
    article_by_part = _article_map([line.part_type_id for line in [*sales, *repairs]])
    returned_sales, returned_repairs = _returned_line_ids(sales, repairs)
    rows = []
    for line in sales:
        row = _row_from_sale(
            line, article_by_part.get(line.part_type_id, ""), line.pk in returned_sales
        )
        if row is not None:
            rows.append(row)
    for line in repairs:
        row = _row_from_repair(
            line, article_by_part.get(line.part_type_id, ""), line.pk in returned_repairs
        )
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda row: (row.document_kind, row.document_date, row.line_id))


def _article_map(part_ids):
    rows = PartNumber.objects.filter(part_id__in=set(part_ids), is_primary=True)
    return dict(rows.values_list("part_id", "value"))


def _returned_line_ids(sales, repairs):
    sale_ids = [line.pk for line in sales]
    repair_ids = [line.pk for line in repairs]
    returned = StockReturnLine.objects.filter(stock_return__status=StockReturn.Status.COMPLETED)
    return (
        set(
            returned.filter(source_sale_line_id__in=sale_ids).values_list(
                "source_sale_line_id", flat=True
            )
        ),
        set(
            returned.filter(source_repair_line_id__in=repair_ids).values_list(
                "source_repair_line_id", flat=True
            )
        ),
    )


def _row_from_sale(line, article, has_return):
    return _build_row(
        kind="sale",
        document=line.sale,
        line=line,
        article=article,
        customer_price=line.unit_price,
        customer_total=line.total_price,
        has_return=has_return,
    )


def _row_from_repair(line, article, has_return):
    price = line.customer_unit_price_rub
    total = None if price is None else price * line.quantity
    return _build_row(
        kind="repair",
        document=line.repair_order,
        line=line,
        article=article,
        customer_price=price,
        customer_total=total,
        has_return=has_return,
    )


def _build_row(*, kind, document, line, article, customer_price, customer_total, has_return):
    if customer_total is None or customer_total >= line.total_cost_rub:
        return None
    source = line.part_item if line.part_item_id else line.stock_lot
    snapshot = source.receipt_customer_price_snapshot_rub
    current = line.part_type.recommended_price
    current_default = resolve_effective_inventory_customer_price(source, current)
    cause, explanation = _classify(
        customer_price=customer_price,
        cost=line.unit_cost_rub,
        current=current,
        snapshot=snapshot,
        current_default=current_default,
        has_return=has_return,
    )
    delta = customer_total - line.total_cost_rub
    margin = None if not line.total_cost_rub else (delta / line.total_cost_rub) * Decimal("100")
    return BelowCostRow(
        document_kind=kind,
        document_id=document.pk,
        document_number=document.number,
        document_date=document.sold_at if kind == "sale" else document.completed_at,
        customer=document.customer_name,
        line_id=line.pk,
        article=article,
        part_name=line.part_type.name,
        quantity=line.quantity,
        source_kind="PartItem" if line.part_item_id else "StockLot",
        source_id=source.pk,
        customer_unit_price_rub=customer_price,
        customer_line_amount_rub=customer_total,
        accounting_unit_cost_rub=line.unit_cost_rub,
        accounting_line_cost_rub=line.total_cost_rub,
        delta_rub=delta,
        margin_percent=margin,
        inventory_history="snapshot-aware" if snapshot is not None else "historical-no-snapshot",
        manual_override_identifiable="not-recorded",
        receipt_customer_price_snapshot_rub=snapshot,
        current_canonical_price_rub=current,
        probable_cause=cause,
        explanation=explanation,
    )


def _classify(*, customer_price, cost, current, snapshot, current_default, has_return):
    if has_return:
        return (
            "F. RETURNS/CANCELLATION_EFFECT",
            "У строки есть проведённый возврат/отмена: проверьте net-эффект документа.",
        )
    if customer_price is not None and customer_price <= 0:
        return (
            "E. PRICE_DATA_ANOMALY",
            "Нулевая или отрицательная цена клиента при положительной себестоимости.",
        )
    if snapshot is None:
        return (
            "C. HISTORICAL_NO_SNAPSHOT",
            "У исторического источника нет достоверного receipt-time снимка цены.",
        )
    if current is not None and snapshot > current and customer_price == current:
        return (
            "A. PRICE_LIST_DROP",
            "Снимок источника выше текущего прайса, а документ использовал текущую цену.",
        )
    if (
        current_default is not None
        and customer_price is not None
        and customer_price < current_default
    ):
        return (
            "B. MANUAL_OVERRIDE",
            "Документная цена ниже текущего default; "
            "флаг ручного ввода исторически не записывался.",
        )
    if cost >= Decimal("1000000"):
        return (
            "D. COST_DATA_ANOMALY",
            "Необычно высокая себестоимость: требуется проверка поступления.",
        )
    return "G. OTHER", "Нужна ручная проверка цены, себестоимости и исходного документа."
