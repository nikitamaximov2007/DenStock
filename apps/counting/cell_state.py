"""Что лежит в ячейке прямо сейчас, по каноническим складским данным.

Экран незавершённой инвентаризации показывал только то, что оператор успел
отсканировать. Если после начала пересчёта в ту же ячейку что-то приняли,
переместили или списали, экран продолжал показывать старое число: он никогда
не спрашивал склад, а считал всё по своим же строкам сканирования. Оператор
видел «7 деталей» там, где физически лежало три десятка.

Здесь ячейка спрашивается у самого склада. Остаток берётся тем же
`live_stock_rows`, что и поиск со сканером; себестоимость считается той же
формулой, что и в статистике склада: экземпляры по `landed_cost_rub`, лоты по
`quantity * landed_unit_cost_rub`. Новой параллельной формулы остатков здесь
нет, и ни одной записи этот модуль не создаёт: только чтение.
"""
from decimal import Decimal

from django.db.models import Q, Sum

from apps.inventory.models import PartItem, StockLot, StockMovement
from apps.inventory.movement import live_stock_rows
from apps.inventory.services import ITEM_PHYSICAL_STATUSES, LOT_PHYSICAL_STATUSES

DEC0 = Decimal("0")


def movements_touching_cell(location, since):
    """Движения, задевшие ячейку после указанного момента.

    Ячейку задевает и приход в неё, и уход из неё, поэтому смотрим обе стороны
    движения. `created_at` проиндексирован, отдельный индекс не нужен.
    """
    if location is None or since is None:
        return StockMovement.objects.none()
    return (
        StockMovement.objects.filter(created_at__gt=since)
        .filter(Q(to_location=location) | Q(from_location=location))
        .order_by("created_at")
    )


def cell_warehouse_cost(location) -> Decimal:
    """Себестоимость содержимого ячейки. Ноль у лота - допустимая цифра."""
    items = (
        PartItem.objects.filter(
            current_location=location, status__in=ITEM_PHYSICAL_STATUSES
        ).aggregate(value=Sum("landed_cost_rub"))["value"]
        or DEC0
    )
    lots = StockLot.objects.filter(
        location=location, status__in=LOT_PHYSICAL_STATUSES, quantity__gt=0
    )
    lot_value = sum(
        ((lot.landed_unit_cost_rub or DEC0) * lot.quantity for lot in lots), DEC0
    )
    return items + lot_value


def cell_state(location, *, since=None) -> dict:
    """Каноническое состояние ячейки и признак изменений после `since`.

    `since` - момент начала пересчёта. Если после него ячейку трогали, оператор
    должен об этом узнать: его подсчёт относится к другому содержимому.
    """
    rows = live_stock_rows(location_id=location.pk) if location is not None else []
    by_part = {row.part_type.pk: row.physical for row in rows}
    changed = movements_touching_cell(location, since)
    changes_count = changed.count() if since is not None else 0
    return {
        "rows": rows,
        "positions": len(rows),
        "quantity": sum((row.physical for row in rows), DEC0),
        "available": sum((row.available for row in rows), DEC0),
        "warehouse_cost": cell_warehouse_cost(location),
        "by_part": by_part,
        "changed": changes_count > 0,
        "changes_count": changes_count,
    }
