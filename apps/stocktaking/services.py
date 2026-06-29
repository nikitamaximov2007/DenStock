"""Слой 20 — сервисы инвентаризации (сверка факта с системой + корректировки).

Единственная точка изменения документа инвентаризации. `apps.stocktaking` ведёт
ДОКУМЕНТ (шапку, строки, снимок expected, факт counted) и оркеструет проведение,
но физику склада НЕ трогает: корректировку остатка (`StockLot.quantity`,
`StockMovement` ADJUST_*, `StockBalance`) делает сервис `apps.inventory`
(`adjust_stock_lot_quantity`).

Это акт сверки, а НЕ списание: при `counted ≠ live` документ приводит количество
лота к факту. Source of truth расхождения — живой `StockLot.quantity` (под
блокировкой при проведении), `expected_quantity` строки — лишь снимок для UI.
Нельзя свести лот ниже активной брони (`active_reserved_for_lot` из `apps.sales`);
авто-отмену резерва не делаем.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.inventory.models import StockLot
from apps.inventory.services import (
    LOT_PHYSICAL_STATUSES,
    InventoryError,
    adjust_stock_lot_quantity,
)
from apps.sales.services import active_reserved_for_lot

from .models import InventoryCountDocument, InventoryCountLine


class StocktakingError(Exception):
    """Невозможно выполнить операцию с документом инвентаризации."""


# --- Создание / наполнение документа -----------------------------------------


def create_inventory_count(*, scope_location=None, comment="", by=None) -> InventoryCountDocument:
    """Создать черновик инвентаризации (склад ещё не трогаем)."""
    return InventoryCountDocument.objects.create(
        scope_location=scope_location, comment=(comment or "").strip(),
        created_by=by, status=InventoryCountDocument.Status.DRAFT,
    )


def _ensure_draft(doc: InventoryCountDocument) -> None:
    if doc.status != InventoryCountDocument.Status.DRAFT:
        raise StocktakingError("Документ уже проведён или отменён — изменять нельзя.")


@transaction.atomic
def add_stock_lot_count_line(doc, lot, *, by=None) -> InventoryCountLine:
    """Добавить строку лота: снимок системного количества (expected) и заморозка
    себестоимости. Инвентаризируем только физические лоты (available/quarantine/
    receiving); один лот — одна строка в документе."""
    doc = InventoryCountDocument.objects.select_for_update().get(pk=doc.pk)
    _ensure_draft(doc)
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status not in LOT_PHYSICAL_STATUSES:
        raise StocktakingError("Инвентаризировать можно только физически присутствующий лот.")
    if InventoryCountLine.objects.filter(count_document=doc, stock_lot=lot).exists():
        raise StocktakingError("Этот лот уже в документе.")
    return InventoryCountLine.objects.create(
        count_document=doc, stock_lot=lot, part_type=lot.part_type,
        batch_line=lot.batch_line, location=lot.location,
        expected_quantity=lot.quantity, unit_cost_rub=lot.landed_unit_cost_rub,
    )


@transaction.atomic
def update_counted_quantity(line, counted, *, by=None) -> InventoryCountLine:
    """Ввести/изменить фактическое количество по строке (только в черновике)."""
    line = (
        InventoryCountLine.objects.select_for_update()
        .select_related("count_document")
        .get(pk=line.pk)
    )
    _ensure_draft(line.count_document)
    counted = Decimal(counted)
    if counted < 0:
        raise StocktakingError("Фактическое количество не может быть отрицательным.")
    line.counted_quantity = counted
    line.save(update_fields=["counted_quantity"])
    return line


@transaction.atomic
def remove_count_line(line, *, by=None) -> None:
    """Снять строку из черновика документа."""
    line = (
        InventoryCountLine.objects.select_for_update()
        .select_related("count_document")
        .get(pk=line.pk)
    )
    _ensure_draft(line.count_document)
    line.delete()


# --- Проведение / отмена -----------------------------------------------------


@transaction.atomic
def complete_inventory_count(doc, *, by=None) -> InventoryCountDocument:
    """Провести инвентаризацию: по каждой строке привести `StockLot.quantity` к
    `counted` через inventory.adjust_stock_lot_quantity. Дельта считается от ЖИВОГО
    количества (под блокировкой). `counted == live` → движения нет. Нельзя свести
    лот ниже активной брони.
    """
    doc = InventoryCountDocument.objects.select_for_update().get(pk=doc.pk)
    if doc.status != InventoryCountDocument.Status.DRAFT:
        raise StocktakingError("Документ уже проведён или отменён.")
    lines = list(doc.lines.select_related("stock_lot", "part_type"))
    if not lines:
        raise StocktakingError("Нельзя провести пустую инвентаризацию.")
    if any(line.counted_quantity is None for line in lines):
        raise StocktakingError("Не все строки сосчитаны — введите фактическое количество.")

    for line in lines:
        lot = StockLot.objects.select_for_update().get(pk=line.stock_lot_id)
        delta = line.counted_quantity - lot.quantity
        if delta == 0:
            continue  # факт совпал с системой — корректировка не нужна
        if delta < 0 and line.counted_quantity < active_reserved_for_lot(lot):
            raise StocktakingError(
                f"Лот #{lot.pk}: факт {line.counted_quantity} меньше зарезервированного "
                f"{active_reserved_for_lot(lot)} — сначала решите бронь."
            )
        try:
            movement = adjust_stock_lot_quantity(
                lot, delta, by=by, comment=f"Инвентаризация {doc.number}",
                document_type="inventory_count", document_id=doc.pk,
            )
        except InventoryError as exc:
            raise StocktakingError(str(exc)) from exc
        line.adjustment = movement
        line.save(update_fields=["adjustment"])

    doc.status = InventoryCountDocument.Status.COMPLETED
    doc.completed_at = timezone.now()
    doc.save(update_fields=["status", "completed_at", "updated_at"])
    return doc


@transaction.atomic
def cancel_inventory_count(doc, *, by=None) -> InventoryCountDocument:
    """Отменить черновик документа (склад не затрагивали — отменять можно только draft)."""
    doc = InventoryCountDocument.objects.select_for_update().get(pk=doc.pk)
    if doc.status == InventoryCountDocument.Status.CANCELED:
        return doc
    if doc.status != InventoryCountDocument.Status.DRAFT:
        raise StocktakingError("Отменить можно только черновик (проведённый документ неизменяем).")
    doc.status = InventoryCountDocument.Status.CANCELED
    doc.canceled_at = timezone.now()
    doc.save(update_fields=["status", "canceled_at", "updated_at"])
    return doc
