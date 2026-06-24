"""Слой 8 — создание поштучных экземпляров (PartItem).

Создаёт физические экземпляры из строки уже финансово закрытой партии. Без
складских движений (`StockMovement`/`StockBalance` — Слой 10) и без сканера.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.procurement.models import BatchLine

from .models import NumberSequence, PartItem, StockLot


class InventoryError(Exception):
    """Невозможно создать экземпляр(ы) детали."""


def _validate_line(line: BatchLine) -> None:
    part = line.part_type
    if part.tracking_mode != part.TrackingMode.SERIAL:
        raise InventoryError("Экземпляры создаются только для поштучных деталей.")
    if not line.batch.cost_finalized:
        raise InventoryError(
            "Себестоимость партии не зафиксирована — создание экземпляров запрещено."
        )
    if line.quantity != line.quantity.to_integral_value():
        raise InventoryError("Для поштучной строки количество должно быть целым.")


def existing_count(line: BatchLine) -> int:
    return PartItem.objects.filter(batch_line=line).count()


@transaction.atomic
def create_part_items(
    line: BatchLine,
    count: int = 1,
    *,
    serial_number: str = "",
    current_location=None,
    note: str = "",
) -> list[PartItem]:
    """Создать `count` экземпляров из строки партии (одиночно или массово).

    Серийный номер применяется только при единичном создании; при массовом
    (`count > 1`) серийники не задаются. Лимит `batch_line.quantity` соблюдается
    под блокировкой строки.
    """
    _validate_line(line)
    if count < 1:
        raise InventoryError("Количество должно быть не меньше 1.")

    # Блокируем строку, чтобы лимит не нарушался при параллельных запросах.
    line = BatchLine.objects.select_for_update().get(pk=line.pk)
    already = PartItem.objects.filter(batch_line=line).count()
    limit = int(line.quantity)
    if already + count > limit:
        raise InventoryError(
            f"Нельзя создать {count}: лимит строки {limit}, уже создано {already}."
        )

    serial = serial_number.strip() if count == 1 else ""
    if serial and PartItem.objects.filter(part_type=line.part_type, serial_number=serial).exists():
        raise InventoryError("Серийный номер уже используется для этой детали.")

    if current_location is not None and not current_location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")

    items: list[PartItem] = []
    for _ in range(count):
        number = NumberSequence.next("part_item")
        item = PartItem(
            internal_number=number,
            internal_barcode=f"ITEM:{number}",
            part_type=line.part_type,
            batch=line.batch,
            batch_line=line,
            serial_number=serial,
            landed_cost_rub=line.landed_unit_cost_rub,
            current_location=current_location,
            note=note,
        )
        item.save()
        items.append(item)
    return items


# --- Количественные лоты (StockLot) -----------------------------------------


def distributed_qty(line: BatchLine) -> Decimal:
    """Сколько количества строки уже распределено по лотам."""
    agg = StockLot.objects.filter(batch_line=line).aggregate(s=Sum("quantity"))
    return agg["s"] or Decimal("0")


def remaining_qty(line: BatchLine) -> Decimal:
    """Нераспределённый остаток строки."""
    return line.quantity - distributed_qty(line)


def _validate_bulk_line(line: BatchLine) -> None:
    part = line.part_type
    if part.tracking_mode != part.TrackingMode.BULK:
        raise InventoryError("Лоты создаются только для количественных деталей.")
    if not line.batch.cost_finalized:
        raise InventoryError(
            "Себестоимость партии не зафиксирована — создание лотов запрещено."
        )


@transaction.atomic
def create_stock_lot(line: BatchLine, location, quantity, *, note: str = "") -> StockLot:
    """Создать количественный лот из строки партии в конкретной ячейке."""
    _validate_bulk_line(line)
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise InventoryError("Количество должно быть больше нуля.")
    if location is None or not location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")

    # Блокируем строку, чтобы лимит соблюдался при параллельных запросах.
    line = BatchLine.objects.select_for_update().get(pk=line.pk)
    already = (
        StockLot.objects.filter(batch_line=line).aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )
    if already + quantity > line.quantity:
        raise InventoryError(
            f"Нельзя распределить {quantity}: остаток строки {line.quantity - already}."
        )
    if StockLot.objects.filter(batch_line=line, location=location).exists():
        raise InventoryError("Лот для этой строки в данной ячейке уже существует.")

    lot = StockLot(
        part_type=line.part_type,
        batch=line.batch,
        batch_line=line,
        location=location,
        quantity=quantity,
        initial_quantity=quantity,
        landed_unit_cost_rub=line.landed_unit_cost_rub,
        note=note,
    )
    lot.save()
    return lot


@transaction.atomic
def update_stock_lot(lot: StockLot, *, location, quantity, note: str = "") -> StockLot:
    """Правка лота до появления движений: место/количество/примечание.

    `initial_quantity` при правке не меняется (фиксируется при создании).
    """
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise InventoryError("Количество должно быть больше нуля.")
    if location is None or not location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")

    line = BatchLine.objects.select_for_update().get(pk=lot.batch_line_id)
    others = (
        StockLot.objects.filter(batch_line=line).exclude(pk=lot.pk)
        .aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )
    if others + quantity > line.quantity:
        raise InventoryError(
            f"Нельзя установить {quantity}: остаток строки {line.quantity - others}."
        )
    if (
        StockLot.objects.filter(batch_line=line, location=location)
        .exclude(pk=lot.pk)
        .exists()
    ):
        raise InventoryError("Лот для этой строки в данной ячейке уже существует.")

    lot.location = location
    lot.quantity = quantity
    lot.note = note
    lot.save(update_fields=["location", "quantity", "note", "updated_at"])
    return lot

