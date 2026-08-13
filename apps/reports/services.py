"""Слой 21 — сервисы-агрегаторы отчётов. СТРОГО read-only.

Функции читают уже созданные документы/движения/кэш остатков и возвращают простые
dataclass-структуры (HTML-агностично, удобно тестировать). Здесь НЕТ ни одной
записи: не создаём документы/движения, не меняем `StockLot`/`StockBalance`/итоги.
Денежные поля считаются всегда; СКРЫВАЕТ их шаблон по `can_view_purchase_cost`.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Sum
from django.db.models.functions import Trim
from django.utils import timezone

from apps.inventory.models import StockBalance, StockMovement
from apps.inventory.presentation import identity_for_part_ids
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
    exact_number: str = ""


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
    exact_number: str = ""
    manufacturer: str = ""


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
    top_rev = list(
        lines.values("part_type_id", "part_type__name")
        .annotate(v=Sum("total_price")).order_by("-v")[:TOP_N]
    )
    top_qty = list(
        lines.values("part_type_id", "part_type__name")
        .annotate(v=Sum("quantity")).order_by("-v")[:TOP_N]
    )
    identity = identity_for_part_ids(
        {r["part_type_id"] for r in top_rev} | {r["part_type_id"] for r in top_qty}
    )
    return SalesReport(
        count=agg["count"] or 0,
        line_count=lines.count(),
        revenue=money(agg["revenue"] or DEC0),
        cost=money(agg["cost"] or DEC0),
        profit=money(agg["profit"] or DEC0),
        top_by_revenue=[
            TopRow(
                r["part_type__name"], money(r["v"] or DEC0),
                identity[r["part_type_id"]].exact_number,
            )
            for r in top_rev
        ],
        top_by_quantity=[
            TopRow(
                r["part_type__name"], r["v"] or DEC0,
                identity[r["part_type_id"]].exact_number,
            )
            for r in top_qty
        ],
    )


def _completed_sale_lines(period: Period):
    """Frozen completed sale lines for customer reports."""
    start, end = _bounds(period)
    return SaleLine.objects.filter(
        sale__status=Sale.Status.COMPLETED,
        sale__sold_at__range=(start, end),
    )


def get_sales_by_customer(period: Period):
    """Aggregate completed line snapshots by the stored customer name."""
    return (
        _completed_sale_lines(period)
        .annotate(report_customer=Trim("sale__customer_name"))
        .values("report_customer")
        .annotate(
            sale_count=Count("sale_id", distinct=True),
            unique_parts=Count("part_type_id", distinct=True),
            quantity=Sum("quantity"),
            revenue=Sum("total_price"),
            last_sale=Max("sale__sold_at"),
        )
        .order_by("-revenue", "report_customer")
    )


def _customer_sale_lines(period: Period, *, customer_name: str, missing: bool):
    customer_name = (customer_name or "").strip()
    expected = "" if missing else customer_name
    return _completed_sale_lines(period).annotate(
        report_customer=Trim("sale__customer_name")
    ).filter(report_customer=expected)


def get_customer_part_sales(period: Period, *, customer_name: str, missing: bool):
    """Aggregate one customer's completed snapshots by current part identity."""
    return (
        _customer_sale_lines(period, customer_name=customer_name, missing=missing)
        .values("part_type_id", "part_type__name")
        .annotate(
            quantity=Sum("quantity"),
            revenue=Sum("total_price"),
            operation_count=Count("sale_id", distinct=True),
            last_sale=Max("sale__sold_at"),
        )
        .order_by("-revenue", "part_type__name", "part_type_id")
    )


def attach_customer_part_identity(rows) -> list[dict]:
    """Attach canonical exact numbers in fixed queries, without per-row lookup."""
    rows = list(rows)
    identities = identity_for_part_ids({row["part_type_id"] for row in rows})
    for row in rows:
        identity = identities.get(row["part_type_id"])
        row["exact_number"] = identity.exact_number if identity else ""
    return rows


def get_customer_part_operations(
    period: Period,
    *,
    customer_name: str,
    missing: bool,
    part_type_id: int,
):
    """Individual completed operations with frozen unit and line prices."""
    return (
        _customer_sale_lines(period, customer_name=customer_name, missing=missing)
        .filter(part_type_id=part_type_id)
        .select_related("sale", "sale__sold_by", "part_type")
        .order_by("-sale__sold_at", "-sale_id", "pk")
    )


# --- Ремонт/выдачи (по completed_at; без выручки — Слой 17 без цены работ) ----


def _completed_repair_lines(period: Period):
    """Строки проведённых ремонтов за период: снимки себестоимости заморожены."""
    start, end = _bounds(period)
    return RepairIssueLine.objects.filter(
        repair_order__status=RepairOrder.Status.COMPLETED,
        repair_order__completed_at__range=(start, end),
    )


def get_repairs_by_customer(period: Period):
    """Ремонты по клиентам за период.

    ВАЖНО про деньги: у ремонтного заказа НЕТ клиентской суммы. Слой 17
    фиксирует, куда ушли детали, и замораживает их СЕБЕСТОИМОСТЬ; цены работ,
    оплаты и прибыли в системе нет. Поэтому здесь считается «себестоимость
    выданного», а не выручка, и называть её выручкой нельзя. Сортировка идёт по
    количеству, а не по деньгам: количество видно всем ролям, а себестоимость
    закрыта правом на закупочные цены.
    """
    return (
        _completed_repair_lines(period)
        .annotate(report_customer=Trim("repair_order__customer_name"))
        .values("report_customer")
        .annotate(
            repair_count=Count("repair_order_id", distinct=True),
            unique_parts=Count("part_type_id", distinct=True),
            quantity=Sum("quantity"),
            issued_cost=Sum("total_cost_rub"),
            last_repair=Max("repair_order__completed_at"),
        )
        .order_by("-quantity", "report_customer")
    )


def _customer_repair_lines(period: Period, *, customer_name: str, missing: bool):
    customer_name = (customer_name or "").strip()
    expected = "" if missing else customer_name
    return (
        _completed_repair_lines(period)
        .annotate(report_customer=Trim("repair_order__customer_name"))
        .filter(report_customer=expected)
    )


def get_customer_repair_parts(period: Period, *, customer_name: str, missing: bool):
    """Детали, выданные одному клиенту в ремонт: количество и себестоимость."""
    return (
        _customer_repair_lines(period, customer_name=customer_name, missing=missing)
        .values("part_type_id", "part_type__name")
        .annotate(
            quantity=Sum("quantity"),
            issued_cost=Sum("total_cost_rub"),
            operation_count=Count("repair_order_id", distinct=True),
            last_repair=Max("repair_order__completed_at"),
        )
        .order_by("-quantity", "part_type__name", "part_type_id")
    )


def get_customer_repair_operations(
    period: Period,
    *,
    customer_name: str,
    missing: bool,
    part_type_id: int,
):
    """Отдельные выдачи в ремонт с замороженной себестоимостью строки."""
    return (
        _customer_repair_lines(period, customer_name=customer_name, missing=missing)
        .filter(part_type_id=part_type_id)
        .select_related("repair_order", "repair_order__created_by", "part_type")
        .order_by("-repair_order__completed_at", "-repair_order_id", "pk")
    )


# --- Клиент целиком: продажи и ремонты вместе (без общей суммы) --------------


def _client_row(name: str) -> dict:
    return {
        "report_customer": name,
        "display_name": name or "Без клиента",
        "sale_count": 0,
        "sale_quantity": DEC0,
        "revenue": DEC0,
        "repair_count": 0,
        "repair_quantity": DEC0,
        "issued_cost": DEC0,
        "last_sale": None,
        "last_repair": None,
    }


def _later(first, second):
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def get_clients_sales_and_repairs(period: Period) -> list[dict]:
    """Клиенты с продажами И ремонтами за период, одной строкой на клиента.

    ОБЩЕЙ ДЕНЕЖНОЙ СУММЫ ЗДЕСЬ НЕТ И БЫТЬ НЕ МОЖЕТ. Выручка продажи это деньги
    клиента, а «себестоимость выданного» в ремонте это закупочная стоимость
    деталей: клиентской суммы у ремонтного заказа система не хранит. Складывать
    их в один итог значило бы выдумать величину, которой в системе нет, поэтому
    деньги показываются двумя отдельными колонками со своими названиями.

    Сортировка идёт по числу документов, а не по деньгам: количество документов
    видно всем ролям, а деньги закрыты правом на закупочные цены.
    """
    rows: dict[str, dict] = {}
    for row in get_sales_by_customer(period):
        name = row["report_customer"] or ""
        entry = rows.setdefault(name, _client_row(name))
        entry["sale_count"] = row["sale_count"] or 0
        entry["sale_quantity"] = row["quantity"] or DEC0
        entry["revenue"] = row["revenue"] or DEC0
        entry["last_sale"] = row["last_sale"]
    for row in get_repairs_by_customer(period):
        name = row["report_customer"] or ""
        entry = rows.setdefault(name, _client_row(name))
        entry["repair_count"] = row["repair_count"] or 0
        entry["repair_quantity"] = row["quantity"] or DEC0
        entry["issued_cost"] = row["issued_cost"] or DEC0
        entry["last_repair"] = row["last_repair"]
    for entry in rows.values():
        entry["document_count"] = entry["sale_count"] + entry["repair_count"]
        entry["last_event"] = _later(entry["last_sale"], entry["last_repair"])
    return sorted(
        rows.values(),
        key=lambda entry: (-entry["document_count"], entry["report_customer"]),
    )


def _client_filter(customer_name: str, missing: bool) -> str:
    return "" if missing else (customer_name or "").strip()


def get_client_timeline(period: Period, *, customer_name: str, missing: bool) -> list[dict]:
    """Единая лента документов клиента: продажи и ремонты по времени проведения.

    Историческая лента: берутся только проведённые документы и их замороженные
    значения. Каждая запись ведёт в исходный документ, поэтому суммы в ленте
    всегда можно сверить с первоисточником.

    Деньги не смешиваются: у продажи это выручка, у ремонта это себестоимость
    выданного, и каждая запись несёт только свою величину.
    """
    start, end = _bounds(period)
    expected = _client_filter(customer_name, missing)

    sales = (
        Sale.objects.filter(status=Sale.Status.COMPLETED, sold_at__range=(start, end))
        .annotate(report_customer=Trim("customer_name"))
        .filter(report_customer=expected)
        .annotate(line_quantity=Sum("lines__quantity"))
        .select_related("sold_by")
    )
    repairs = (
        RepairOrder.objects.filter(
            status=RepairOrder.Status.COMPLETED, completed_at__range=(start, end)
        )
        .annotate(report_customer=Trim("customer_name"))
        .filter(report_customer=expected)
        .annotate(line_quantity=Sum("lines__quantity"))
        .select_related("created_by")
    )

    events = [
        {
            "kind": "sale",
            "kind_label": "Продажа",
            "document_id": sale.pk,
            "number": sale.number,
            "at": sale.sold_at,
            "quantity": sale.line_quantity or DEC0,
            "revenue": sale.revenue_total,
            "issued_cost": None,
            "employee": sale.sold_by,
            "note": sale.comment,
        }
        for sale in sales
    ] + [
        {
            "kind": "repair",
            "kind_label": "Ремонт",
            "document_id": order.pk,
            "number": order.number,
            "at": order.completed_at,
            "quantity": order.line_quantity or DEC0,
            "revenue": None,
            "issued_cost": order.cost_total,
            "employee": order.created_by,
            "note": " ".join(
                part
                for part in (order.vehicle_make, order.vehicle_model, order.vehicle_identifier)
                if part
            ),
        }
        for order in repairs
    ]
    events.sort(key=lambda event: (event["at"] is None, event["at"]), reverse=True)
    return events


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
    low = [
        r for r in rows
        if (r["part_type__min_stock_level"] or DEC0) > 0
        and (r["available"] or DEC0) < (r["part_type__min_stock_level"] or DEC0)
    ]
    identity = identity_for_part_ids({r["part_type"] for r in low})
    return [
        LowStockRow(
            r["part_type__name"],
            r["available"] or DEC0,
            r["part_type__min_stock_level"] or DEC0,
            identity[r["part_type"]].exact_number,
            identity[r["part_type"]].manufacturer,
        )
        for r in low
    ]


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
