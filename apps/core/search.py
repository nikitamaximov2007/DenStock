"""Быстрый поиск детали (Слой 13).

`search_parts(query)` — ЧИСТЫЙ read-only сервис: по строке находит виды деталей
(`PartType`) и считает наличие. Ничего не пишет: ни движений, ни `StockBalance`,
ни статусов. Наличие берётся из кэша `StockBalance`, с откатом на первичку
(`PartItem`/`StockLot`), БЕЗ удвоения:

  - если у детали есть строки `StockBalance` → берём ТОЛЬКО кэш;
  - если строк кэша по детали нет → считаем ТОЛЬКО из первички;
  - кэш и первичку никогда не складываем вместе.

`receiving` (на приёмке) — всегда из первички, как отдельный под-показатель
(не суммируется с `physical`).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, Q, Sum

from apps.catalog.models import (
    PartBarcode,
    PartCompatibility,
    PartNumber,
    PartType,
    normalize_number,
)
from apps.inventory.models import PartItem, StockBalance, StockLot
from apps.inventory.services import ITEM_PHYSICAL_STATUSES, LOT_PHYSICAL_STATUSES

MIN_QUERY_LEN = 2
RESULT_LIMIT = 30


@dataclass
class PartSearchRow:
    part: PartType
    physical: object = Decimal("0")
    available: object = Decimal("0")
    reserved: object = Decimal("0")
    receiving: object = Decimal("0")
    locations: list = field(default_factory=list)
    batches: list = field(default_factory=list)
    source: str = "balance"  # "balance" | "primary" — откуда взято наличие
    # Разворот (заполняет вьюха только для инвентарь-видящих ролей).
    items: list = field(default_factory=list)
    lots: list = field(default_factory=list)


def _matched_part_ids(q: str, nq: str) -> set[int]:
    """ID видов деталей, совпавших по любому из полей поиска."""
    ids: set[int] = set()
    ids.update(PartType.objects.filter(name__icontains=q).values_list("pk", flat=True))
    if nq:
        ids.update(
            PartNumber.objects.filter(normalized_value__icontains=nq)
            .values_list("part_id", flat=True)
        )
    ids.update(PartBarcode.objects.filter(value__icontains=q).values_list("part_id", flat=True))
    ids.update(
        PartItem.objects.filter(
            Q(internal_number__icontains=q)
            | Q(internal_barcode__icontains=q)
            | Q(serial_number__icontains=q)
        ).values_list("part_type_id", flat=True)
    )
    ids.update(
        PartCompatibility.objects.filter(
            Q(vehicle_model__name__icontains=q)
            | Q(vehicle_model__vehicle_make__name__icontains=q)
        ).values_list("part_id", flat=True)
    )
    return ids


def search_parts(query: str) -> list[PartSearchRow]:
    q = (query or "").strip()
    if len(q) < MIN_QUERY_LEN:
        return []
    nq = normalize_number(q)

    ids = _matched_part_ids(q, nq)
    if not ids:
        return []
    parts = list(
        PartType.objects.filter(pk__in=ids)
        .select_related("category", "manufacturer", "unit")
        .prefetch_related("numbers")
        .order_by("name")[:RESULT_LIMIT]
    )
    part_ids = [p.pk for p in parts]

    # --- Кэш StockBalance по детали (первичный источник наличия) ---
    balance = {
        r["part_type_id"]: r
        for r in StockBalance.objects.filter(part_type_id__in=part_ids)
        .values("part_type_id")
        .annotate(
            physical=Sum("quantity_physical"),
            available=Sum("quantity_available"),
            reserved=Sum("quantity_reserved"),
        )
    }
    bal_locs: dict[int, set] = defaultdict(set)
    bal_batches: dict[int, set] = defaultdict(set)
    for pid, code, num in (
        StockBalance.objects.filter(part_type_id__in=part_ids)
        .values_list("part_type_id", "location__code", "batch__number")
    ):
        bal_locs[pid].add(code)
        bal_batches[pid].add(num)

    # --- Откат на первичку ТОЛЬКО для деталей без строк кэша ---
    fallback_ids = [pid for pid in part_ids if pid not in balance]
    prim = _primary_aggregate(fallback_ids) if fallback_ids else {}

    # --- receiving (всегда из первички, как отдельный показатель) ---
    receiving = _receiving_counts(part_ids)

    rows: list[PartSearchRow] = []
    for part in parts:
        pid = part.pk
        if pid in balance:
            phys = balance[pid]["physical"] or Decimal("0")
            avail = balance[pid]["available"] or Decimal("0")
            resv = balance[pid]["reserved"] or Decimal("0")
            locs = sorted(c for c in bal_locs[pid] if c)
            batches = sorted(b for b in bal_batches[pid] if b)
            source = "balance"
        else:
            # Резерв держит только физически присутствующий остаток, у которого
            # есть строки кэша; в первичном fallback резерв = 0 (так core не
            # импортирует sales).
            data = prim.get(pid, {})
            phys = data.get("physical", Decimal("0"))
            avail = data.get("available", Decimal("0"))
            resv = Decimal("0")
            locs = sorted(data.get("locations", set()))
            batches = sorted(data.get("batches", set()))
            source = "primary"
        rows.append(
            PartSearchRow(
                part=part, physical=phys, available=avail, reserved=resv,
                receiving=receiving.get(pid, Decimal("0")),
                locations=locs, batches=batches, source=source,
            )
        )
    return rows


def _primary_aggregate(part_ids: list[int]) -> dict[int, dict]:
    """Наличие из первички (для деталей без кэша). Без удвоения с кэшем."""
    result: dict[int, dict] = {
        pid: {
            "physical": Decimal("0"), "available": Decimal("0"),
            "locations": set(), "batches": set(),
        }
        for pid in part_ids
    }

    # Поштучные экземпляры.
    items = PartItem.objects.filter(
        part_type_id__in=part_ids,
        status__in=ITEM_PHYSICAL_STATUSES,
        current_location__isnull=False,
    ).values_list("part_type_id", "status", "current_location__code", "batch__number")
    for pid, status, code, num in items:
        cell = result[pid]
        cell["physical"] += 1
        if status != PartItem.Status.QUARANTINE:
            cell["available"] += 1
        cell["locations"].add(code)
        cell["batches"].add(num)

    # Количественные лоты.
    lots = StockLot.objects.filter(
        part_type_id__in=part_ids, status__in=LOT_PHYSICAL_STATUSES,
    ).values_list("part_type_id", "status", "quantity", "location__code", "batch__number")
    for pid, status, qty, code, num in lots:
        cell = result[pid]
        cell["physical"] += qty
        if status != StockLot.Status.QUARANTINE:
            cell["available"] += qty
        cell["locations"].add(code)
        cell["batches"].add(num)

    return result


def _receiving_counts(part_ids: list[int]) -> dict[int, object]:
    """Количество на приёмке (status=receiving) из первички, по детали."""
    receiving: dict[int, object] = defaultdict(lambda: Decimal("0"))
    items = (
        PartItem.objects.filter(part_type_id__in=part_ids, status=PartItem.Status.RECEIVING)
        .values("part_type_id").annotate(n=Count("id"))
    )
    for r in items:
        receiving[r["part_type_id"]] += r["n"]
    lots = (
        StockLot.objects.filter(part_type_id__in=part_ids, status=StockLot.Status.RECEIVING)
        .values("part_type_id").annotate(s=Sum("quantity"))
    )
    for r in lots:
        receiving[r["part_type_id"]] += r["s"] or Decimal("0")
    return receiving
