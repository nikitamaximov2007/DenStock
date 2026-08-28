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
from apps.repairs.services import (
    repair_customer_amounts,
    repair_customer_line_amounts,
    repair_returned_quantities,
)
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
        revenue=Sum("revenue_total"),
        cost=Sum("cost_total"),
        profit=Sum("profit_total"),
    )
    lines = SaleLine.objects.filter(sale__in=sales)
    top_rev = list(
        lines.values("part_type_id", "part_type__name")
        .annotate(v=Sum("total_price"))
        .order_by("-v")[:TOP_N]
    )
    top_qty = list(
        lines.values("part_type_id", "part_type__name")
        .annotate(v=Sum("quantity"))
        .order_by("-v")[:TOP_N]
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
                r["part_type__name"],
                money(r["v"] or DEC0),
                identity[r["part_type_id"]].exact_number,
            )
            for r in top_rev
        ],
        top_by_quantity=[
            TopRow(
                r["part_type__name"],
                r["v"] or DEC0,
                identity[r["part_type_id"]].exact_number,
            )
            for r in top_qty
        ],
    )


# --- Идентичность клиента в отчётах ------------------------------------------
#
# После появления справочника у клиента есть стабильный идентификатор, но вся
# история до него связи не имеет. Поэтому отчёты РАЗДЕЛЯЮТ два случая и никогда
# их не смешивают:
#
# * документ связан с карточкой - строка отчёта это КАРТОЧКА, группировка идёт
#   по её PK. Переименование карточки не дробит строку, потому что снимок имени
#   в группировке не участвует;
# * документ не связан - строка отчёта это ИСТОРИЧЕСКАЯ ЗАПИСЬ, группировка идёт
#   по сохранённому имени. Такие строки помечаются как «без карточки», потому
#   что одинаковое имя не доказывает, что это один человек.


def _linked_rows(lines, *, prefix: str, aggregates: dict):
    """Агрегаты по карточкам клиентов (только связанные документы)."""
    field = f"{prefix}__customer_id"
    return (
        lines.filter(**{f"{prefix}__customer__isnull": False})
        .values(field, f"{prefix}__customer__name")
        .annotate(**aggregates)
    )


def _legacy_rows(lines, *, prefix: str, aggregates: dict):
    """Агрегаты по историческому имени (документы без карточки)."""
    return (
        lines.filter(**{f"{prefix}__customer__isnull": True})
        .annotate(report_customer=Trim(f"{prefix}__customer_name"))
        .values("report_customer")
        .annotate(**aggregates)
    )


def _identity_row(row, *, prefix: str, linked: bool) -> dict:
    """Привести строку агрегата к единому виду с явной пометкой источника."""
    if linked:
        customer_id = row[f"{prefix}__customer_id"]
        name = row[f"{prefix}__customer__name"]
        return {
            **row,
            "customer_id": customer_id,
            "report_customer": name,
            "display_name": name,
            "linked": True,
            "missing_name": False,
        }
    name = row.get("report_customer") or ""
    return {
        **row,
        "customer_id": None,
        "report_customer": name,
        "display_name": name or "Без клиента",
        "linked": False,
        "missing_name": not name,
    }


def _customer_rows(lines, *, prefix: str, aggregates: dict, order_key) -> list[dict]:
    rows = [
        _identity_row(row, prefix=prefix, linked=True)
        for row in _linked_rows(lines, prefix=prefix, aggregates=aggregates)
    ]
    rows += [
        _identity_row(row, prefix=prefix, linked=False)
        for row in _legacy_rows(lines, prefix=prefix, aggregates=aggregates)
    ]
    rows.sort(key=order_key)
    return rows


def _document_customer_filter(prefix: str, *, customer_id, customer_name: str, missing: bool):
    """Фильтр «документы этого клиента» для связанной карточки или legacy-имени."""
    if customer_id:
        return {f"{prefix}__customer_id": customer_id}
    expected = "" if missing else (customer_name or "").strip()
    return {f"{prefix}__customer__isnull": True, "report_customer": expected}


def _completed_sale_lines(period: Period):
    """Frozen completed sale lines for customer reports."""
    start, end = _bounds(period)
    return SaleLine.objects.filter(
        sale__status=Sale.Status.COMPLETED,
        sale__sold_at__range=(start, end),
    )


SALE_CUSTOMER_AGGREGATES = {
    "sale_count": Count("sale_id", distinct=True),
    "unique_parts": Count("part_type_id", distinct=True),
    "quantity": Sum("quantity"),
    "revenue": Sum("total_price"),
    "last_sale": Max("sale__sold_at"),
}


def get_sales_by_customer(period: Period) -> list[dict]:
    """Продажи по клиентам: карточки отдельно, документы без карточки отдельно."""
    rows = _customer_rows(
        _completed_sale_lines(period),
        prefix="sale",
        aggregates=SALE_CUSTOMER_AGGREGATES,
        order_key=lambda row: (-(row["revenue"] or DEC0), row["display_name"]),
    )
    sale_lines = list(
        _completed_sale_lines(period).select_related("sale").only(
            "id", "sale_id", "quantity", "unit_price", "sale__customer_id", "sale__customer_name"
        )
    )
    returned = _sale_returned_quantities(sale_lines)
    totals: dict[tuple, Decimal] = {}
    quantities: dict[tuple, Decimal] = {}
    for line in sale_lines:
        key = (
            ("card", line.sale.customer_id)
            if line.sale.customer_id
            else ("legacy", line.sale.customer_name.strip())
        )
        remaining = max(line.quantity - (returned.get(line.pk) or DEC0), DEC0)
        quantities[key] = quantities.get(key, DEC0) + remaining
        totals[key] = totals.get(key, DEC0) + money(line.unit_price * remaining)
    for row in rows:
        key = ("card", row["customer_id"]) if row["linked"] else ("legacy", row["report_customer"])
        row["quantity"] = quantities.get(key, DEC0)
        row["revenue"] = money(totals.get(key, DEC0))
    return rows


def _sale_returned_quantities(lines):
    """Completed return quantities keyed by completed sale line."""
    from apps.returns.models import StockReturnLine

    lines = list(lines)
    line_ids = [line.pk for line in lines]
    if not line_ids:
        return {}
    return dict(
        StockReturnLine.objects.filter(
            stock_return__status="completed", source_sale_line_id__in=line_ids
        )
        .values("source_sale_line_id")
        .annotate(quantity=Sum("quantity"))
        .values_list("source_sale_line_id", "quantity")
    )


def _customer_sale_lines(
    period: Period, *, customer_name: str = "", missing: bool = False, customer_id=None
):
    return (
        _completed_sale_lines(period)
        .annotate(report_customer=Trim("sale__customer_name"))
        .filter(
            **_document_customer_filter(
                "sale", customer_id=customer_id, customer_name=customer_name, missing=missing
            )
        )
    )


def get_customer_part_sales(
    period: Period, *, customer_name: str = "", missing: bool = False, customer_id=None
):
    """Aggregate one customer's completed snapshots by current part identity."""
    return (
        _customer_sale_lines(
            period, customer_name=customer_name, missing=missing, customer_id=customer_id
        )
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
    part_type_id: int | None = None,
    customer_name: str = "",
    missing: bool = False,
    customer_id=None,
):
    """Individual completed operations with frozen unit and line prices.

    Без ``part_type_id`` возвращается вся история клиента одной плоской лентой:
    строка на каждую проданную деталь, новые сверху. Именно это нужно на
    вопрос «что мы продавали этому клиенту»: документ продажи здесь лишний
    уровень, а суммы взяты из снимка проведённой продажи и не пересчитываются
    по сегодняшнему каталогу.
    """
    lines = _customer_sale_lines(
        period, customer_name=customer_name, missing=missing, customer_id=customer_id
    )
    if part_type_id is not None:
        lines = lines.filter(part_type_id=part_type_id)
    return lines.select_related("sale", "sale__sold_by", "part_type").order_by(
        "-sale__sold_at", "-sale_id", "pk"
    )


def attach_line_reversals(lines):
    """Проставить строкам, сколько из них вернулось и сколько ещё действует.

    Продажа остаётся неизменной: её количество это снимок. Действующее
    количество считается вычитанием канонических возвратов, поэтому историю
    по-прежнему можно доказать - продано столько, отменено столько, осталось
    столько.
    """
    from apps.returns.models import StockReturn, StockReturnLine

    lines = list(lines)
    if not lines:
        return lines
    returned = {
        row["source_sale_line_id"]: row["quantity"]
        for row in (
            StockReturnLine.objects.filter(
                stock_return__status=StockReturn.Status.COMPLETED,
                source_sale_line_id__in=[line.pk for line in lines],
            )
            .values("source_sale_line_id")
            .annotate(quantity=Sum("quantity"))
        )
    }
    for line in lines:
        line.reversed_quantity = returned.get(line.pk) or DEC0
        line.effective_quantity = line.quantity - line.reversed_quantity
        line.reversible_quantity = line.effective_quantity
        line.effective_total = money(line.unit_price * line.effective_quantity)
    return lines


def attach_line_part_identity(lines):
    """Проставить артикул строкам документов одним запросом на страницу.

    Идентичность детали живёт отдельно от строки документа, поэтому артикул
    добирается пакетно: иначе плоская история клиента дала бы запрос на строку.
    """
    lines = list(lines)
    identities = identity_for_part_ids({line.part_type_id for line in lines})
    for line in lines:
        identity = identities.get(line.part_type_id)
        line.exact_number = identity.exact_number if identity else ""
    return lines


# --- Ремонт/выдачи (по completed_at; без выручки — Слой 17 без цены работ) ----


def _completed_repair_lines(period: Period):
    """Строки проведённых ремонтов за период: снимки себестоимости заморожены."""
    start, end = _bounds(period)
    return RepairIssueLine.objects.filter(
        repair_order__status=RepairOrder.Status.COMPLETED,
        repair_order__completed_at__range=(start, end),
    )


REPAIR_CUSTOMER_AGGREGATES = {
    "repair_count": Count("repair_order_id", distinct=True),
    "unique_parts": Count("part_type_id", distinct=True),
    "quantity": Sum("quantity"),
    "issued_cost": Sum("total_cost_rub"),
    "last_repair": Max("repair_order__completed_at"),
}


def get_repairs_by_customer(period: Period) -> list[dict]:
    """Ремонты по клиентам за период.

    Клиентская сумма деталей берётся из исторической цены строки ремонта, а
    себестоимость остаётся отдельной величиной с ограниченным доступом. Цена
    работ, оплаты и прибыли в системе не хранится. Сортировка идёт по
    количеству, а не по деньгам.
    """
    rows = _customer_rows(
        _completed_repair_lines(period),
        prefix="repair_order",
        aggregates=REPAIR_CUSTOMER_AGGREGATES,
        order_key=lambda row: (-(row["quantity"] or DEC0), row["display_name"]),
    )
    orders = list(
        RepairOrder.objects.filter(
            status=RepairOrder.Status.COMPLETED,
            completed_at__range=_bounds(period),
        ).only("id", "customer_id", "customer_name")
    )
    amounts = repair_customer_amounts(orders)
    repair_lines = list(
        RepairIssueLine.objects.filter(repair_order__in=orders).only(
            "id", "repair_order_id", "quantity", "unit_cost_rub"
        )
    )
    returned = repair_returned_quantities(repair_lines)
    order_by_id = {order.pk: order for order in orders}
    totals: dict[tuple, Decimal] = {}
    quantities: dict[tuple, Decimal] = {}
    costs: dict[tuple, Decimal] = {}
    unknown: set[tuple] = set()
    for order in orders:
        key = (
            ("card", order.customer_id)
            if order.customer_id
            else ("legacy", order.customer_name.strip())
        )
        amount = amounts[order.pk]
        if amount is None:
            unknown.add(key)
        else:
            totals[key] = totals.get(key, DEC0) + amount
    for line in repair_lines:
        order = order_by_id[line.repair_order_id]
        key = (
            ("card", order.customer_id)
            if order.customer_id
            else ("legacy", order.customer_name.strip())
        )
        remaining = max(line.quantity - (returned.get(line.pk) or DEC0), DEC0)
        quantities[key] = quantities.get(key, DEC0) + remaining
        costs[key] = costs.get(key, DEC0) + line.unit_cost_rub * remaining
    for row in rows:
        key = ("card", row["customer_id"]) if row["linked"] else ("legacy", row["report_customer"])
        row["quantity"] = quantities.get(key, DEC0)
        row["issued_cost"] = money(costs.get(key, DEC0))
        row["repair_customer_amount"] = money(totals.get(key, DEC0))
        row["repair_customer_amount_unknown"] = key in unknown
    return rows


def _customer_repair_lines(
    period: Period, *, customer_name: str = "", missing: bool = False, customer_id=None
):
    return (
        _completed_repair_lines(period)
        .annotate(report_customer=Trim("repair_order__customer_name"))
        .filter(
            **_document_customer_filter(
                "repair_order",
                customer_id=customer_id,
                customer_name=customer_name,
                missing=missing,
            )
        )
    )


def get_customer_repair_parts(
    period: Period, *, customer_name: str = "", missing: bool = False, customer_id=None
):
    """Детали, выданные одному клиенту в ремонт: количество и себестоимость."""
    return (
        _customer_repair_lines(
            period, customer_name=customer_name, missing=missing, customer_id=customer_id
        )
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
    part_type_id: int | None = None,
    customer_name: str = "",
    missing: bool = False,
    customer_id=None,
):
    """Отдельные выдачи в ремонт с историческими суммой клиента и себестоимостью.

    Без ``part_type_id`` возвращается вся история выдач клиенту одной плоской
    лентой, новые сверху.

    Клиентская сумма не включает работы; себестоимость показывается отдельно и
    только пользователям с правом на закупочные цены.
    """
    lines = _customer_repair_lines(
        period, customer_name=customer_name, missing=missing, customer_id=customer_id
    )
    if part_type_id is not None:
        lines = lines.filter(part_type_id=part_type_id)
    return lines.select_related("repair_order", "repair_order__created_by", "part_type").order_by(
        "-repair_order__completed_at", "-repair_order_id", "pk"
    )


# --- Клиент целиком: продажи и ремонты вместе ---------------------------------


def _client_row(name: str) -> dict:
    return {
        "report_customer": name,
        "display_name": name or "Без клиента",
        "customer_id": None,
        "linked": False,
        "sale_count": 0,
        "sale_quantity": DEC0,
        "revenue": DEC0,
        "repair_count": 0,
        "repair_quantity": DEC0,
        "issued_cost": DEC0,
        "repair_customer_amount": DEC0,
        "repair_customer_amount_unknown": False,
        "client_total_known": DEC0,
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

    Итог с клиента - это выручка продаж плюс историческая стоимость деталей в
    ремонтах. Себестоимость ремонта не входит в этот итог и показывается
    отдельно только пользователям с правом на закупочные цены. Цена работ,
    оплаты и прибыли в системе не хранится.
    """
    # Ключ строки это идентичность клиента, а не текст: карточка объединяется по
    # PK, а документы без карточки остаются отдельными историческими строками.
    rows: dict[tuple, dict] = {}

    def _entry(row):
        key = ("card", row["customer_id"]) if row["linked"] else ("legacy", row["report_customer"])
        entry = rows.get(key)
        if entry is None:
            entry = _client_row(row["report_customer"])
            entry["customer_id"] = row["customer_id"]
            entry["linked"] = row["linked"]
            entry["display_name"] = row["display_name"]
            rows[key] = entry
        return entry

    for row in get_sales_by_customer(period):
        entry = _entry(row)
        entry["sale_count"] = row["sale_count"] or 0
        entry["sale_quantity"] = row["quantity"] or DEC0
        entry["revenue"] = row["revenue"] or DEC0
        entry["last_sale"] = row["last_sale"]
    for row in get_repairs_by_customer(period):
        entry = _entry(row)
        entry["repair_count"] = row["repair_count"] or 0
        entry["repair_quantity"] = row["quantity"] or DEC0
        entry["issued_cost"] = row["issued_cost"] or DEC0
        entry["repair_customer_amount"] = row["repair_customer_amount"] or DEC0
        entry["repair_customer_amount_unknown"] = row["repair_customer_amount_unknown"]
        entry["last_repair"] = row["last_repair"]
    for entry in rows.values():
        entry["client_total_known"] = money(entry["revenue"] + entry["repair_customer_amount"])
        entry["client_total_unknown"] = entry["repair_customer_amount_unknown"]
        entry["document_count"] = entry["sale_count"] + entry["repair_count"]
        entry["last_event"] = _later(entry["last_sale"], entry["last_repair"])
    return sorted(
        rows.values(),
        key=lambda entry: (-entry["document_count"], entry["display_name"]),
    )


CLIENTS_SORT_DOCUMENTS = "documents"
CLIENTS_SORT_DATE = "date"
CLIENTS_SORTS = (CLIENTS_SORT_DOCUMENTS, CLIENTS_SORT_DATE)


def order_clients_rows(rows: list[dict], *, sort: str, direction: str) -> list[dict]:
    """Порядок строк отчёта по клиентам. Суммы от порядка не зависят.

    Дата это `last_event` - момент последнего документа клиента, попавшего в
    отчёт: максимум из даты продажи и даты завершения ремонта. Обе величины
    уже посчитаны агрегатом в базе, поэтому сортировка не делает ни одного
    дополнительного запроса.

    При равных датах порядок задаётся именем клиента по алфавиту в обоих
    направлениях: иначе одинаковые даты выстраивались бы произвольно и
    страницы «поехали» бы между запросами. Строки без даты уходят в конец
    тоже в обоих направлениях: «нет даты» это не «очень давно».
    """
    if sort != CLIENTS_SORT_DATE:
        return rows
    dated = [entry for entry in rows if entry["last_event"] is not None]
    undated = [entry for entry in rows if entry["last_event"] is None]
    dated.sort(key=lambda entry: entry["display_name"])
    dated.sort(key=lambda entry: entry["last_event"], reverse=direction != "asc")
    undated.sort(key=lambda entry: entry["display_name"])
    return dated + undated


def _client_filter(customer_name: str, missing: bool) -> str:
    return "" if missing else (customer_name or "").strip()


def get_client_part_history(
    period: Period, *, customer_name: str = "", missing: bool = False, customer_id=None
) -> list[dict]:
    """Плоская история клиента: строка на каждую деталь, продажи и ремонты вместе.

    Отвечает на вопрос «что мы давали этому клиенту и когда». Документ здесь не
    показывается: он лишний уровень между вопросом и ответом.

    У продажи и ремонта есть историческая сумма для клиента. Для ремонта она
    покрывает только детали, а себестоимость остаётся отдельным складским
    показателем. Неизвестная цена старой строки остаётся ``None``.
    """
    sale_lines = attach_line_part_identity(
        get_customer_part_operations(
            period, customer_name=customer_name, missing=missing, customer_id=customer_id
        )
    )
    repair_lines = attach_line_part_identity(
        get_customer_repair_operations(
            period, customer_name=customer_name, missing=missing, customer_id=customer_id
        )
    )
    repair_amounts = repair_customer_line_amounts(repair_lines)
    repair_returns = repair_returned_quantities(repair_lines)

    rows = [
        {
            "kind": "sale",
            "kind_label": "Продажа",
            "at": line.sale.sold_at,
            "part_type_id": line.part_type_id,
            "part_name": line.part_type.name,
            "exact_number": line.exact_number,
            "quantity": line.quantity,
            "amount": line.total_price,
            "cost": None,
        }
        for line in sale_lines
    ]
    for line in repair_lines:
        net_quantity = max(line.quantity - (repair_returns.get(line.pk) or DEC0), DEC0)
        rows.append(
            {
            "kind": "repair",
            "kind_label": "Ремонт",
            "at": line.repair_order.completed_at,
            "part_type_id": line.part_type_id,
            "part_name": line.part_type.name,
            "exact_number": line.exact_number,
            "quantity": net_quantity,
            "amount": repair_amounts[line.pk],
            # Заморожено при проведении заказа и не пересчитывается по
            # сегодняшнему каталогу: это историческая себестоимость выдачи.
            "cost": money(line.unit_cost_rub * net_quantity),
        }
        )
    # Новые сверху. Вторичный ключ по названию делает порядок устойчивым, когда
    # несколько строк проведены одним документом в одну и ту же секунду.
    rows.sort(key=lambda row: (row["at"], row["part_name"]), reverse=True)
    return rows


def get_client_timeline(
    period: Period, *, customer_name: str = "", missing: bool = False, customer_id=None
) -> list[dict]:
    """Единая лента документов клиента: продажи и ремонты по времени проведения.

    Ни один экран этой лентой больше не пользуется: клиентская карточка
    показывает сразу строки деталей, см. ``get_client_part_history``. Функция
    остаётся как документная проекция того же периода и держит проверки
    идентичности клиента, где важен именно документ, а не строка.

    Историческая лента: берутся только проведённые документы и их замороженные
    значения. Каждая запись ведёт в исходный документ, поэтому суммы в ленте
    всегда можно сверить с первоисточником.

    У продажи это выручка, у ремонта - историческая сумма деталей для клиента.
    Себестоимость ремонта остаётся отдельной величиной.
    """
    start, end = _bounds(period)
    # Карточка выбрана - берём её документы по связи. Карточки нет - только
    # документы без связи с тем же историческим именем.
    if customer_id:
        document_filter = {"customer_id": customer_id}
    else:
        document_filter = {
            "customer__isnull": True,
            "report_customer": "" if missing else (customer_name or "").strip(),
        }

    sales = (
        Sale.objects.filter(status=Sale.Status.COMPLETED, sold_at__range=(start, end))
        .annotate(report_customer=Trim("customer_name"))
        .filter(**document_filter)
        .annotate(line_quantity=Sum("lines__quantity"))
        .select_related("sold_by")
    )
    repairs = (
        RepairOrder.objects.filter(
            status=RepairOrder.Status.COMPLETED, completed_at__range=(start, end)
        )
        .annotate(report_customer=Trim("customer_name"))
        .filter(**document_filter)
        .annotate(line_quantity=Sum("lines__quantity"))
        .select_related("created_by")
    )
    repair_amounts = repair_customer_amounts(repairs)

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
            "revenue": repair_amounts[order.pk],
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
        .values("part_type__name")
        .annotate(v=Sum("quantity"))
        .order_by("-v")[:TOP_N]
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
        .values("part_type__name")
        .annotate(v=Sum("quantity"))
        .order_by("-v")[:TOP_N]
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
    ain = lines.filter(adjustment__movement_type=StockMovement.MovementType.ADJUST_IN).aggregate(
        qty=Sum("adjustment__quantity"), cost=Sum("adjustment__total_cost_rub")
    )
    aout = lines.filter(adjustment__movement_type=StockMovement.MovementType.ADJUST_OUT).aggregate(
        qty=Sum("adjustment__quantity"), cost=Sum("adjustment__total_cost_rub")
    )
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
                r["location__code"],
                r["available"] or DEC0,
                r["reserved"] or DEC0,
                r["quarantine"] or DEC0,
            )
            for r in by_loc
        ],
    )


def get_low_stock_report() -> list:
    rows = (
        StockBalance.objects.values("part_type", "part_type__name", "part_type__min_stock_level")
        .annotate(available=Sum("quantity_available"))
        .order_by("part_type__name")
    )
    low = [
        r
        for r in rows
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
