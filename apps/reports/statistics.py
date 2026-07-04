"""Layer 27 — «Статистика»: текущее состояние склада (СТРОГО read-only).

Отличие от отчётов (services.py): отчёты отвечают «что произошло за период»,
статистика — «что лежит на складе сейчас и на что обратить внимание». Здесь нет
ни одной записи в БД: только агрегаты по существующим таблицам (StockBalance,
StockLot, PartItem, StockMovement, Reservation, Sale). Новых моделей нет.

Период (7/30/90/всё время) влияет ТОЛЬКО на «ходовые позиции», «активность» и
порог «залежавшихся»; стоимость склада и низкие остатки — точечный срез сейчас.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Sum
from django.utils import timezone

from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.procurement.models import money
from apps.sales.models import Reservation, Sale, SaleLine

TOP_N = 10
STALE_FALLBACK_DAYS = 90  # порог «залежался» для пресета «всё время»
EXPIRING_SOON_DAYS = 7
DEC0 = Decimal("0")
_MONEY = DecimalField(max_digits=20, decimal_places=2)

STATS_PRESETS = [("7", "7 дней"), ("30", "30 дней"), ("90", "90 дней"), ("all", "Всё время")]


# --- Период (проще отчётного: только пресеты) ---------------------------------


@dataclass
class StatsPeriod:
    preset: str
    days: int | None  # None == всё время
    label: str


def resolve_stats_period(get) -> StatsPeriod:
    """?period=7|30|90|all; любой другой ввод -> дефолт «30 дней»."""
    preset = (get.get("period") or "").strip()
    labels = dict(STATS_PRESETS)
    if preset == "all":
        return StatsPeriod("all", None, labels["all"])
    if preset in ("7", "30", "90"):
        return StatsPeriod(preset, int(preset), labels[preset])
    return StatsPeriod("30", 30, labels["30"])


def _since(period: StatsPeriod):
    if period.days is None:
        return None
    return timezone.now() - timedelta(days=period.days)


# --- Структуры -----------------------------------------------------------------


@dataclass
class StatsKpi:
    stock_cost: Decimal
    potential_revenue: Decimal
    part_types_with_stock: int
    total_available: Decimal
    active_reservations: int
    low_stock_count: int
    stale_count: int
    locations_with_stock: int
    unvalued_count: int


@dataclass
class ValueRow:
    name: str
    value: Decimal


@dataclass
class PartValueRow:
    pk: int
    name: str
    value: Decimal


@dataclass
class LowStockRow:
    pk: int
    name: str
    available: Decimal
    min_stock_level: Decimal


@dataclass
class StaleRow:
    pk: int
    name: str
    available: Decimal
    value: Decimal
    last_movement: object  # datetime | None (None == движений не было)


@dataclass
class MoverRow:
    name: str
    quantity: Decimal
    revenue: Decimal
    profit: Decimal


@dataclass
class ActivityRow:
    label: str
    count: int


@dataclass
class Statistics:
    period: StatsPeriod
    kpi: StatsKpi
    value_by_category: list = field(default_factory=list)
    top_parts_by_value: list = field(default_factory=list)
    low_stock: list = field(default_factory=list)
    stale: list = field(default_factory=list)
    movers: list = field(default_factory=list)
    attention_reservations: list = field(default_factory=list)
    activity: list = field(default_factory=list)
    activity_total: int = 0


# --- Деньги в складе (срез сейчас; себестоимость из landed cost) ---------------


def _value_maps():
    """Стоимость доступного остатка: по видам деталей и по категориям.

    Экземпляры: сумма landed_cost_rub доступных. Лоты: quantity * unit_cost
    доступных. Один проход по каждой таблице, слияние в Python (видов деталей
    на складе немного относительно движений).
    """
    by_part: dict[int, dict] = {}
    by_category: dict[str, Decimal] = {}

    items = (
        PartItem.objects.filter(status=PartItem.Status.AVAILABLE)
        .values("part_type_id", "part_type__name", "part_type__category__name")
        .annotate(v=Sum("landed_cost_rub"))
    )
    lot_value = ExpressionWrapper(
        F("quantity") * F("landed_unit_cost_rub"), output_field=_MONEY
    )
    lots = (
        StockLot.objects.filter(status=StockLot.Status.AVAILABLE, quantity__gt=0)
        .values("part_type_id", "part_type__name", "part_type__category__name")
        .annotate(v=Sum(lot_value))
    )
    for row in list(items) + list(lots):
        value = row["v"] or DEC0
        part = by_part.setdefault(
            row["part_type_id"], {"name": row["part_type__name"], "value": DEC0}
        )
        part["value"] += value
        cat = row["part_type__category__name"] or "Без категории"
        by_category[cat] = by_category.get(cat, DEC0) + value
    return by_part, by_category


def _unvalued_count() -> int:
    """Доступные позиции без себестоимости (landed cost == 0) — «не оценено»."""
    items = PartItem.objects.filter(
        status=PartItem.Status.AVAILABLE, landed_cost_rub=0
    ).count()
    lots = StockLot.objects.filter(
        status=StockLot.Status.AVAILABLE, quantity__gt=0, landed_unit_cost_rub=0
    ).count()
    return items + lots


def _potential_revenue() -> Decimal:
    """Доступно × рекомендованная цена (только там, где цена задана)."""
    expr = ExpressionWrapper(
        F("quantity_available") * F("part_type__recommended_price"), output_field=_MONEY
    )
    agg = StockBalance.objects.filter(
        part_type__recommended_price__isnull=False
    ).aggregate(v=Sum(expr))
    return money(agg["v"] or DEC0)


# --- Низкие остатки (та же логика, что в отчётах, + pk для ссылки) -------------


def _low_stock() -> list:
    rows = (
        StockBalance.objects.values(
            "part_type_id", "part_type__name", "part_type__min_stock_level"
        )
        .annotate(available=Sum("quantity_available"))
        .order_by("part_type__name")
    )
    result = []
    for r in rows:
        minlvl = r["part_type__min_stock_level"] or DEC0
        avail = r["available"] or DEC0
        if minlvl > 0 and avail < minlvl:
            result.append(
                LowStockRow(r["part_type_id"], r["part_type__name"], avail, minlvl)
            )
    return result


# --- Залежавшиеся позиции -------------------------------------------------------


def _stale(period: StatsPeriod, by_part_value: dict) -> list:
    """Виды деталей с остатком, по которым нет движений дольше порога.

    Порог = период страницы (7/30/90 дней); для «всё время» — 90 дней.
    """
    days = period.days or STALE_FALLBACK_DAYS
    cutoff = timezone.now() - timedelta(days=days)
    stocked = (
        StockBalance.objects.values("part_type_id", "part_type__name")
        .annotate(phys=Sum("quantity_physical"), avail=Sum("quantity_available"))
        .filter(phys__gt=0)
    )
    last_by_part = dict(
        StockMovement.objects.values_list("part_type_id")
        .annotate(last=Max("created_at"))
        .values_list("part_type_id", "last")
    )
    rows = []
    for r in stocked:
        last = last_by_part.get(r["part_type_id"])
        if last is not None and last >= cutoff:
            continue
        value = by_part_value.get(r["part_type_id"], {}).get("value", DEC0)
        rows.append(
            StaleRow(
                r["part_type_id"], r["part_type__name"],
                r["avail"] or DEC0, money(value), last,
            )
        )
    rows.sort(key=lambda x: x.value, reverse=True)
    return rows[: TOP_N * 2]


# --- Ходовые позиции (за период; те же данные, что у отчёта продаж) -------------


def _movers(period: StatsPeriod) -> list:
    lines = SaleLine.objects.filter(sale__status=Sale.Status.COMPLETED)
    since = _since(period)
    if since is not None:
        lines = lines.filter(sale__sold_at__gte=since)
    top = (
        lines.values("part_type__name")
        .annotate(qty=Sum("quantity"), revenue=Sum("total_price"), profit=Sum("profit_rub"))
        .order_by("-qty")[:TOP_N]
    )
    return [
        MoverRow(
            r["part_type__name"], r["qty"] or DEC0,
            money(r["revenue"] or DEC0), money(r["profit"] or DEC0),
        )
        for r in top
    ]


# --- Резервы, требующие внимания -------------------------------------------------


def _attention_reservations() -> list:
    now = timezone.now()
    soon = now + timedelta(days=EXPIRING_SOON_DAYS)
    active = (
        Reservation.objects.filter(status=Reservation.Status.ACTIVE)
        .order_by(F("expires_at").asc(nulls_last=True))[: TOP_N]
    )
    rows = []
    for r in active:
        expired = bool(r.expires_at and r.expires_at < now)
        expiring = bool(r.expires_at and now <= r.expires_at <= soon)
        rows.append({"reservation": r, "expired": expired, "expiring_soon": expiring})
    return rows


# --- Активность склада (за период) ----------------------------------------------

_ACTIVITY_BUCKETS = [
    ("Приёмка", ("receive_item", "receive_lot")),
    ("Перемещения", ("move_item", "move_lot")),
    ("Продажи", ("sale_item", "sale_lot")),
    ("Выдачи в ремонт", ("issue_item", "issue_lot")),
    ("Возвраты", ("return_item", "return_lot")),
    ("Списания", ("write_off_item", "write_off_lot")),
    ("Корректировки", ("adjust_in", "adjust_out")),
]


def _activity(period: StatsPeriod):
    movements = StockMovement.objects.all()
    since = _since(period)
    if since is not None:
        movements = movements.filter(created_at__gte=since)
    counts = dict(
        movements.values_list("movement_type").annotate(c=Count("id"))
        .values_list("movement_type", "c")
    )
    rows = [
        ActivityRow(label, sum(counts.get(t, 0) for t in types))
        for label, types in _ACTIVITY_BUCKETS
    ]
    return rows, sum(counts.values())


# --- Сборка ----------------------------------------------------------------------


def get_statistics(period: StatsPeriod) -> Statistics:
    by_part, by_category = _value_maps()
    stock_cost = money(sum((p["value"] for p in by_part.values()), DEC0))

    balances = StockBalance.objects.all()
    agg = balances.aggregate(avail=Sum("quantity_available"))
    part_types_with_stock = (
        balances.filter(quantity_physical__gt=0).values("part_type").distinct().count()
    )
    locations_with_stock = (
        balances.filter(quantity_physical__gt=0).values("location").distinct().count()
    )

    low_stock = _low_stock()
    stale = _stale(period, by_part)
    movers = _movers(period)
    attention = _attention_reservations()
    activity, activity_total = _activity(period)

    categories = sorted(
        (ValueRow(name, money(v)) for name, v in by_category.items()),
        key=lambda r: r.value, reverse=True,
    )[:TOP_N]
    top_parts = sorted(
        (PartValueRow(pk, p["name"], money(p["value"])) for pk, p in by_part.items()),
        key=lambda r: r.value, reverse=True,
    )[:TOP_N]

    kpi = StatsKpi(
        stock_cost=stock_cost,
        potential_revenue=_potential_revenue(),
        part_types_with_stock=part_types_with_stock,
        total_available=agg["avail"] or DEC0,
        active_reservations=Reservation.objects.filter(
            status=Reservation.Status.ACTIVE
        ).count(),
        low_stock_count=len(low_stock),
        stale_count=len(stale),
        locations_with_stock=locations_with_stock,
        unvalued_count=_unvalued_count(),
    )
    return Statistics(
        period=period,
        kpi=kpi,
        value_by_category=categories,
        top_parts_by_value=top_parts,
        low_stock=low_stock,
        stale=stale,
        movers=movers,
        attention_reservations=attention,
        activity=activity,
        activity_total=activity_total,
    )
