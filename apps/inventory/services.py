"""Слой 8 — создание поштучных экземпляров (PartItem).

Создаёт физические экземпляры из строки уже финансово закрытой партии. Без
складских движений (`StockMovement`/`StockBalance` — Слой 10) и без сканера.
"""
from django.db import transaction

from apps.procurement.models import BatchLine

from .models import NumberSequence, PartItem


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
