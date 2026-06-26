"""Слой 19 — сервисы документированного списания со склада.

Единственная точка изменения документа списания. `apps.writeoffs` ведёт ДОКУМЕНТ
(шапку, строки, причину, заморозку себестоимости) и оркеструет проведение, но
физику склада НЕ трогает: выбытие (`PartItem.status`/`StockLot.quantity`,
`StockMovement`, `StockBalance`) делают сервисы `apps.inventory`
(`write_off_part_item`/`write_off_stock_lot_quantity`).

Списать можно `available` ИЛИ `quarantine` (брак/карантин — главные кандидаты), но
НЕ зарезервированное: резерв-проверки берём из публичного API `apps.sales`
(`is_part_item_reserved`/`active_reserved_for_lot`). Авто-отмену брони не делаем —
резерв снимают отдельно до списания.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.inventory.models import PartItem, StockLot
from apps.inventory.services import write_off_part_item, write_off_stock_lot_quantity
from apps.procurement.models import money
from apps.sales.services import active_reserved_for_lot, is_part_item_reserved

from .models import WriteOffDocument, WriteOffLine

# Исходные статусы, которые можно списать (физически на складе).
_ITEM_WRITE_OFF_SOURCES = (PartItem.Status.AVAILABLE, PartItem.Status.QUARANTINE)
_LOT_WRITE_OFF_SOURCES = (StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE)


class WriteOffError(Exception):
    """Невозможно выполнить операцию с документом списания."""


# --- Заморозка себестоимости -------------------------------------------------


def _freeze_write_off_line_cost(line: WriteOffLine) -> None:
    """Заморозить себестоимость строки на момент списания (из landed cost объекта)."""
    if line.part_item_id:
        unit_cost = line.part_item.landed_cost_rub
    else:
        unit_cost = line.stock_lot.landed_unit_cost_rub
    line.unit_cost_rub = unit_cost
    line.total_cost_rub = money(unit_cost * line.quantity)


# --- Создание / наполнение документа -----------------------------------------


def create_write_off(*, reason, comment="", by=None) -> WriteOffDocument:
    """Создать черновик списания (склад ещё не трогаем)."""
    if reason not in WriteOffDocument.Reason.values:
        raise WriteOffError("Не указана корректная причина списания.")
    return WriteOffDocument.objects.create(
        reason=reason, comment=(comment or "").strip(),
        created_by=by, status=WriteOffDocument.Status.DRAFT,
    )


def _ensure_draft(doc: WriteOffDocument) -> None:
    if doc.status != WriteOffDocument.Status.DRAFT:
        raise WriteOffError("Документ уже проведён или отменён — изменять состав нельзя.")


@transaction.atomic
def add_part_item_to_write_off(doc, item, *, note="", by=None) -> WriteOffLine:
    """Добавить экземпляр в документ (целиком, quantity = 1)."""
    doc = WriteOffDocument.objects.select_for_update().get(pk=doc.pk)
    _ensure_draft(doc)
    item = PartItem.objects.select_for_update().get(pk=item.pk)
    if item.status not in _ITEM_WRITE_OFF_SOURCES:
        raise WriteOffError("Списать можно только доступный или карантинный экземпляр.")
    if WriteOffLine.objects.filter(write_off=doc, part_item=item).exists():
        raise WriteOffError("Этот экземпляр уже в этом документе.")
    if is_part_item_reserved(item):
        raise WriteOffError("Экземпляр зарезервирован — сначала снимите бронь.")
    return WriteOffLine.objects.create(
        write_off=doc, part_type=item.part_type, part_item=item,
        batch=item.batch, batch_line=item.batch_line,
        quantity=Decimal("1"), note=(note or "").strip(),
    )


@transaction.atomic
def add_stock_lot_to_write_off(doc, lot, quantity, *, note="", by=None) -> WriteOffLine:
    """Добавить количество из лота. Доступно = qty − активный_резерв − уже в документе."""
    doc = WriteOffDocument.objects.select_for_update().get(pk=doc.pk)
    _ensure_draft(doc)
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise WriteOffError("Количество должно быть больше нуля.")
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status not in _LOT_WRITE_OFF_SOURCES:
        raise WriteOffError("Списать можно только доступный или карантинный лот.")
    reserved = active_reserved_for_lot(lot)
    already_in_doc = (
        WriteOffLine.objects.filter(write_off=doc, stock_lot=lot)
        .aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )
    available = lot.quantity - reserved - already_in_doc
    if quantity > available:
        raise WriteOffError(
            f"Недостаточно в лоте: доступно к списанию {available}, запрошено {quantity}."
        )
    return WriteOffLine.objects.create(
        write_off=doc, part_type=lot.part_type, stock_lot=lot,
        batch=lot.batch, batch_line=lot.batch_line,
        quantity=quantity, note=(note or "").strip(),
    )


@transaction.atomic
def remove_write_off_line(line, *, by=None) -> None:
    """Снять позицию из черновика документа."""
    line = (
        WriteOffLine.objects.select_for_update().select_related("write_off").get(pk=line.pk)
    )
    _ensure_draft(line.write_off)
    line.delete()


# --- Проведение / отмена -----------------------------------------------------


@transaction.atomic
def complete_write_off(doc, *, by=None) -> WriteOffDocument:
    """Провести списание: выбыть остаток через inventory.write_off_*, заморозить
    себестоимость. На проведении заново проверяем доступность и резерв каждой
    строки — если деталь успели продать/выдать/зарезервировать, падаем с ошибкой.
    """
    doc = WriteOffDocument.objects.select_for_update().get(pk=doc.pk)
    if doc.status != WriteOffDocument.Status.DRAFT:
        raise WriteOffError("Документ уже проведён или отменён.")
    lines = list(doc.lines.select_related("part_item", "stock_lot", "part_type"))
    if not lines:
        raise WriteOffError("Нельзя провести пустое списание.")

    now = timezone.now()
    for line in lines:
        if line.part_item_id:
            item = PartItem.objects.select_for_update().get(pk=line.part_item_id)
            if item.status not in _ITEM_WRITE_OFF_SOURCES:
                raise WriteOffError(f"Экземпляр {item.internal_number} нельзя списать.")
            if is_part_item_reserved(item):
                raise WriteOffError(
                    f"Экземпляр {item.internal_number} зарезервирован — снимите бронь."
                )
            line.part_item = item
            _freeze_write_off_line_cost(line)
            line.written_off_at = now
            line.save(update_fields=["unit_cost_rub", "total_cost_rub", "written_off_at"])
            write_off_part_item(
                item, by=by, document_id=doc.pk, comment=f"Списание {doc.number}"
            )
        else:
            lot = StockLot.objects.select_for_update().get(pk=line.stock_lot_id)
            if lot.status not in _LOT_WRITE_OFF_SOURCES:
                raise WriteOffError(f"Лот #{lot.pk} нельзя списать.")
            reserved = active_reserved_for_lot(lot)
            if line.quantity > lot.quantity - reserved:
                raise WriteOffError(
                    f"Лот #{lot.pk}: доступно к списанию {lot.quantity - reserved}, "
                    f"нужно {line.quantity}."
                )
            line.stock_lot = lot
            _freeze_write_off_line_cost(line)
            line.written_off_at = now
            line.save(update_fields=["unit_cost_rub", "total_cost_rub", "written_off_at"])
            write_off_stock_lot_quantity(
                lot, line.quantity, by=by, document_id=doc.pk,
                comment=f"Списание {doc.number}",
            )

    doc.cost_total = calculate_write_off_costs(doc)
    doc.status = WriteOffDocument.Status.COMPLETED
    doc.completed_at = now
    doc.save(update_fields=["cost_total", "status", "completed_at", "updated_at"])
    return doc


@transaction.atomic
def cancel_write_off(doc, *, by=None) -> WriteOffDocument:
    """Отменить черновик документа (склад не затрагивали — отменять можно только draft)."""
    doc = WriteOffDocument.objects.select_for_update().get(pk=doc.pk)
    if doc.status == WriteOffDocument.Status.CANCELED:
        return doc
    if doc.status != WriteOffDocument.Status.DRAFT:
        raise WriteOffError("Отменить можно только черновик (проведённое списание неизменяемо).")
    doc.status = WriteOffDocument.Status.CANCELED
    doc.canceled_at = timezone.now()
    doc.save(update_fields=["status", "canceled_at", "updated_at"])
    return doc


def calculate_write_off_costs(doc: WriteOffDocument) -> Decimal:
    """Сумма себестоимости из (замороженных) строк документа."""
    total = doc.lines.aggregate(s=Sum("total_cost_rub"))["s"] or Decimal("0")
    return money(total)
