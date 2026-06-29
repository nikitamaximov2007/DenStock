"""Слой 21 — сервисы-агрегаторы отчётов. СТРОГО read-only.

Функции читают уже созданные документы/движения/кэш остатков и возвращают простые
dataclass-структуры (HTML-агностично, удобно тестировать). Здесь НЕТ ни одной
записи: не создаём документы/движения, не меняем `StockLot`/`StockBalance`/итоги.
Денежные поля считаются всегда; СКРЫВАЕТ их шаблон по `can_view_purchase_cost`.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from apps.inventory.models import StockBalance, StockMovement
from apps.procurement.models import money
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.returns.models import StockReturn
from apps.sales.models import Sale, SaleLine
from apps.stocktaking.models import InventoryCountDocument, InventoryCountLine
from apps.writeoffs.models import WriteOffDocument, WriteOffLine

TOP_N = 10
DEC0 = Decimal("0")


# --- Период ------------------------------------------------------------------


@dataclass
class Period:
    date_from: date
    date_to: date
    preset: str  # "today"/"7"/"30"/"month"/"" (ручной)


def _parse_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def resolve_period(get) -> Period:
    """Разобрать период из query (?preset= или ?date_from=&date_to=). Любой
    некорректный ввод → дефолт «последние 30 дней»; from>to нормализуем."""
    today = timezone.localdate()
    preset = (get.get("preset") or "").strip()
    if preset == "today":
        return Period(today, today, "today")
    if preset == "7":
        return Period(today - timedelta(days=6), today, "7")
    if preset == "30":
        return Period(today - timedelta(days=29), today, "30")
    if preset == "month":
        return Period(today.replace(day=1), today, "month")
    df, dt = _parse_date(get.get("date_from")), _parse_date(get.get("date_to"))
    if df and dt:
        if df > dt:
            df, dt = dt, df
        return Period(df, dt, "")
    return Period(today - timedelta(days=29), today, "30")  # дефолт


def _bounds(period: Period):
    """Границы периода как aware-datetime [from 00:00, to 23:59:59.999999]."""
    start = timezone.make_aware(datetime.combine(period.date_from, time.min))
    end = timezone.make_aware(datetime.combine(period.date_to, time.max))
    return start, end


# --- Структуры отчётов -------------------------------------------------------


@dataclass
class TopRow:
    part_type: str
    value: Decimal


@dataclass
class SalesReport:
    count: int
    line_count: int
    revenue: Decimal
    cost: Decimal
    profit: Decimal
    top_by_revenue: list = field(default_factory=list)
    top_by_quantity: list = field(default_factory=list)


@dataclass
class RepairReport:
    count: int
    issued_cost: Decimal
    top_parts: list = field(default_factory=list)


@dataclass
class ReturnsReport:
    count: int
    quantity: Decimal
    cost: Decimal


@dataclass
class WriteoffReasonRow:
    reason: str
    count: int
    cost: Decimal


@dataclass
class WriteoffReport:
    count: int
    cost: Decimal
    by_reason: list = field(default_factory=list)
    top_parts: list = field(default_factory=list)


@dataclass
class AdjustmentsReport:
    count: int
    adjust_in_qty: Decimal
    adjust_in_cost: Decimal
    adjust_out_qty: Decimal
    adjust_out_cost: Decimal


@dataclass
class StockLocationRow:
    location: str
    available: Decimal
    reserved: Decimal
    quarantine: Decimal


@dataclass
class StockReport:
    part_types_with_stock: int
    total_available: Decimal
    total_reserved: Decimal
    total_quarantine: Decimal
    by_location: list = field(default_factory=list)


@dataclass
class LowStockRow:
    part_type: str
    available: Decimal
    min_stock_level: Decimal


@dataclass
class DashboardReport:
    period: Period
    sales: SalesReport
    repairs: RepairReport
    returns: ReturnsReport
    writeoffs: WriteoffReport
    adjustments: AdjustmentsReport


# --- Продажи (по sold_at, только completed) ----------------------------------


def get_sales_report(period: Period) -> SalesReport:
    start, end = _bounds(period)
    sales = Sale.objects.filter(status=Sale.Status.COMPLETED, sold_at__range=(start, end))
    agg = sales.aggregate(
        count=Count("id"),
        revenue=Sum("revenue_total"), cost=Sum("cost_total"), profit=Sum("profit_total"),
    )
    lines = SaleLine.objects.filter(sale__in=sales)
    top_rev = (
        lines.values("part_type__name").annotate(v=Sum("total_price")).order_by("-v")[:TOP_N]
    )
    top_qty = (
        lines.values("part_type__name").annotate(v=Sum("quantity")).order_by("-v")[:TOP_N]
    )
    return SalesReport(
        count=agg["count"] or 0,
        line_count=lines.count(),
        revenue=money(agg["revenue"] or DEC0),
        cost=money(agg["cost"] or DEC0),
        profit=money(agg["profit"] or DEC0),
        top_by_revenue=[TopRow(r["part_type__name"], money(r["v"] or DEC0)) for r in top_rev],
        top_by_quantity=[TopRow(r["part_type__name"], r["v"] or DEC0) for r in top_qty],
    )


# --- Ремонт/выдачи (по completed_at; без выручки — Слой 17 без цены работ) ----


def get_repairs_report(period: Period) -> RepairReport:
    start, end = _bounds(period)
    orders = RepairOrder.objects.filter(
        status=RepairOrder.Status.COMPLETED, completed_at__range=(start, end)
    )
    agg = orders.aggregate(count=Count("id"), cost=Sum("cost_total"))
    top = (
        RepairIssueLine.objects.filter(repair_order__in=orders)
        .values("part_type__name").annotate(v=Sum("quantity")).order_by("-v")[:TOP_N]
    )
    return RepairReport(
        count=agg["count"] or 0,
        issued_cost=money(agg["cost"] or DEC0),
        top_parts=[TopRow(r["part_type__name"], r["v"] or DEC0) for r in top],
    )


# --- Возвраты (отдельно; НЕ вычитаются из выручки) ---------------------------


def get_returns_report(period: Period) -> ReturnsReport:
    start, end = _bounds(period)
    rets = StockReturn.objects.filter(
        status=StockReturn.Status.COMPLETED, completed_at__range=(start, end)
    )
    agg = rets.aggregate(count=Count("id"), cost=Sum("cost_total"))
    qty = StockReturn.objects.filter(
        status=StockReturn.Status.COMPLETED, completed_at__range=(start, end)
    ).aggregate(q=Sum("lines__quantity"))["q"]
    return ReturnsReport(
        count=agg["count"] or 0,
        quantity=qty or DEC0,
        cost=money(agg["cost"] or DEC0),
    )


# --- Списания (по reason; не смешиваем с инвентаризацией) --------------------


def get_writeoffs_report(period: Period) -> WriteoffReport:
    start, end = _bounds(period)
    docs = WriteOffDocument.objects.filter(
        status=WriteOffDocument.Status.COMPLETED, completed_at__range=(start, end)
    )
    agg = docs.aggregate(count=Count("id"), cost=Sum("cost_total"))
    by_reason = (
        docs.values("reason").annotate(c=Count("id"), cost=Sum("cost_total")).order_by("reason")
    )
    reason_labels = dict(WriteOffDocument.Reason.choices)
    top = (
        WriteOffLine.objects.filter(write_off__in=docs)
        .values("part_type__name").annotate(v=Sum("quantity")).order_by("-v")[:TOP_N]
    )
    return WriteoffReport(
        count=agg["count"] or 0,
        cost=money(agg["cost"] or DEC0),
        by_reason=[
            WriteoffReasonRow(
                reason_labels.get(r["reason"], r["reason"]), r["c"], money(r["cost"] or DEC0)
            )
            for r in by_reason
        ],
        top_parts=[TopRow(r["part_type__name"], r["v"] or DEC0) for r in top],
    )


# --- Инвентаризация (ADJUST_IN/OUT отдельно от WRITE_OFF_*) -------------------


def get_stocktaking_report(period: Period) -> AdjustmentsReport:
    start, end = _bounds(period)
    count = InventoryCountDocument.objects.filter(
        status=InventoryCountDocument.Status.COMPLETED, completed_at__range=(start, end)
    ).count()
    lines = InventoryCountLine.objects.filter(
        count_document__status=InventoryCountDocument.Status.COMPLETED,
        count_document__completed_at__range=(start, end),
        adjustment__isnull=False,
    )
    ain = lines.filter(
        adjustment__movement_type=StockMovement.MovementType.ADJUST_IN
    ).aggregate(qty=Sum("adjustment__quantity"), cost=Sum("adjustment__total_cost_rub"))
    aout = lines.filter(
        adjustment__movement_type=StockMovement.MovementType.ADJUST_OUT
    ).aggregate(qty=Sum("adjustment__quantity"), cost=Sum("adjustment__total_cost_rub"))
    return AdjustmentsReport(
        count=count,
        adjust_in_qty=ain["qty"] or DEC0,
        adjust_in_cost=money(ain["cost"] or DEC0),
        adjust_out_qty=aout["qty"] or DEC0,
        adjust_out_cost=money(aout["cost"] or DEC0),
    )


# --- Остатки (точечный срез; StockBalance только читаем) ---------------------


def get_stock_report() -> StockReport:
    balances = StockBalance.objects.all()
    agg = balances.aggregate(
        avail=Sum("quantity_available"),
        res=Sum("quantity_reserved"),
        quar=Sum("quantity_quarantine"),
    )
    part_types_with_stock = (
        balances.filter(quantity_physical__gt=0).values("part_type").distinct().count()
    )
    by_loc = (
        balances.values("location__code")
        .annotate(
            available=Sum("quantity_available"),
            reserved=Sum("quantity_reserved"),
            quarantine=Sum("quantity_quarantine"),
        )
        .order_by("location__code")
    )
    return StockReport(
        part_types_with_stock=part_types_with_stock,
        total_available=agg["avail"] or DEC0,
        total_reserved=agg["res"] or DEC0,
        total_quarantine=agg["quar"] or DEC0,
        by_location=[
            StockLocationRow(
                r["location__code"], r["available"] or DEC0,
                r["reserved"] or DEC0, r["quarantine"] or DEC0,
            )
            for r in by_loc
        ],
    )


def get_low_stock_report() -> list:
    rows = (
        StockBalance.objects.values(
            "part_type", "part_type__name", "part_type__min_stock_level"
        )
        .annotate(available=Sum("quantity_available"))
        .order_by("part_type__name")
    )
    result = []
    for r in rows:
        minlvl = r["part_type__min_stock_level"] or DEC0
        avail = r["available"] or DEC0
        if minlvl > 0 and avail < minlvl:
            result.append(LowStockRow(r["part_type__name"], avail, minlvl))
    return result


# --- Дашборд (сборка периодных отчётов) --------------------------------------


def get_dashboard_report(period: Period) -> DashboardReport:
    return DashboardReport(
        period=period,
        sales=get_sales_report(period),
        repairs=get_repairs_report(period),
        returns=get_returns_report(period),
        writeoffs=get_writeoffs_report(period),
        adjustments=get_stocktaking_report(period),
    )
