"""Layer 28 — сервисы поступления. Единственная точка мутаций документа.

Черновик НЕ трогает склад: ни партий, ни лотов/экземпляров, ни движений, ни
остатков. Всё складское происходит только в `post_receipt` и только через
СУЩЕСТВУЮЩИЕ сервисы (procurement.finalize_cost, inventory.create_*/receive_*),
в одной транзакции: ошибка любой строки откатывает всё (никакого «половинного»
прихода). Повторное проведение запрещено (select_for_update + статус).
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import PartType
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine, money
from apps.procurement.services import finalize_cost

from .models import Receipt, ReceiptLine


class ReceiptError(Exception):
    """Нарушение правил документа поступления."""


def _ensure_draft(receipt: Receipt) -> None:
    if receipt.status != Receipt.Status.DRAFT:
        raise ReceiptError("Проведённое поступление изменять нельзя.")


def _validate_line_values(part_type, quantity, unit_cost_rub, location) -> Decimal:
    """Общая проверка позиции. Возвращает нормализованное количество."""
    if part_type is None:
        raise ReceiptError("Не выбрана деталь.")
    if location is None or not location.can_hold_stock():
        raise ReceiptError("Ячейка не предназначена для хранения остатка.")
    try:
        quantity = Decimal(quantity)
    except (InvalidOperation, TypeError) as exc:
        raise ReceiptError("Некорректное количество.") from exc
    if quantity <= 0:
        raise ReceiptError("Количество должно быть больше нуля.")
    if unit_cost_rub is None or Decimal(unit_cost_rub) < 0:
        raise ReceiptError("Себестоимость не может быть отрицательной.")
    if (
        part_type.tracking_mode == PartType.TrackingMode.SERIAL
        and quantity != quantity.to_integral_value()
    ):
        raise ReceiptError(
            f"«{part_type}» учитывается поштучно: количество должно быть целым."
        )
    return quantity


def create_receipt(*, supplier=None, received_at=None, comment="", by=None) -> Receipt:
    return Receipt.objects.create(
        supplier=supplier,
        received_at=received_at or timezone.localdate(),
        comment=(comment or "").strip(),
        created_by=by,
    )


def update_receipt(receipt: Receipt, *, supplier, received_at, comment) -> Receipt:
    _ensure_draft(receipt)
    receipt.supplier = supplier
    receipt.received_at = received_at
    receipt.comment = (comment or "").strip()
    receipt.save(update_fields=["supplier", "received_at", "comment"])
    return receipt


def add_line(
    receipt: Receipt, *, part_type, quantity, unit_cost_rub, location, comment=""
) -> ReceiptLine:
    _ensure_draft(receipt)
    quantity = _validate_line_values(part_type, quantity, unit_cost_rub, location)
    return ReceiptLine.objects.create(
        receipt=receipt,
        part_type=part_type,
        quantity=quantity,
        unit_cost_rub=money(unit_cost_rub),
        location=location,
        comment=(comment or "").strip(),
    )


def update_line(
    line: ReceiptLine, *, part_type, quantity, unit_cost_rub, location, comment=""
) -> ReceiptLine:
    _ensure_draft(line.receipt)
    quantity = _validate_line_values(part_type, quantity, unit_cost_rub, location)
    line.part_type = part_type
    line.quantity = quantity
    line.unit_cost_rub = money(unit_cost_rub)
    line.location = location
    line.comment = (comment or "").strip()
    line.save(update_fields=["part_type", "quantity", "unit_cost_rub", "location", "comment"])
    return line


def remove_line(line: ReceiptLine) -> None:
    _ensure_draft(line.receipt)
    line.delete()


def receipt_totals(receipt: Receipt) -> dict:
    """Позиции / суммарное количество / сумма себестоимости (для summary)."""
    lines = list(receipt.lines.all())
    total_qty = sum((line.quantity for line in lines), Decimal("0"))
    total_cost = sum((line.quantity * line.unit_cost_rub for line in lines), Decimal("0"))
    return {"lines": len(lines), "quantity": total_qty, "cost": money(total_cost)}


@transaction.atomic
def post_receipt(receipt: Receipt, *, by=None) -> Receipt:
    """Провести поступление: партия + себестоимость + остатки, всё или ничего.

    Идемпотентность: документ блокируется, повторное проведение запрещено.
    Складские мутации — только через существующие сервисы (движения и остатки
    создают receive_part_item / receive_stock_lot, не этот код).
    """
    receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status != Receipt.Status.DRAFT:
        raise ReceiptError("Поступление уже проведено.")
    lines = list(receipt.lines.select_related("part_type", "location"))
    if not lines:
        raise ReceiptError("Нельзя провести поступление без позиций.")
    if receipt.supplier_id is None:
        raise ReceiptError("Выберите поставщика: партия создаётся от поставщика.")
    for line in lines:
        _validate_line_values(
            line.part_type, line.quantity, line.unit_cost_rub, line.location
        )

    # 1. Партия из документа (валюта RUB, курс 1: цены вводятся в рублях).
    batch = Batch.objects.create(
        supplier=receipt.supplier,
        arrived_at=receipt.received_at,
        notes=f"Создана из поступления {receipt.number}",
        created_by=by,
    )
    for line in lines:
        line.batch_line = BatchLine.objects.create(
            batch=batch,
            part_type=line.part_type,
            quantity=line.quantity,
            unit_cost_currency=line.unit_cost_rub,
            note=line.comment,
        )
        line.save(update_fields=["batch_line"])

    # 2. Фиксация себестоимости существующим сервисом (доп. расходов нет,
    #    поэтому landed cost за единицу равен введённой цене).
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status", "updated_at"])
    batch = finalize_cost(batch, by)  # возвращает свежий экземпляр (select_for_update)

    # 3. Приход на склад существующими сервисами (движения + остатки там).
    for line in lines:
        line.batch_line.refresh_from_db()
        note = f"Поступление {receipt.number}"
        if line.part_type.tracking_mode == PartType.TrackingMode.SERIAL:
            items = create_part_items(line.batch_line, int(line.quantity))
            for item in items:
                receive_part_item(item, to_location=line.location, by=by, comment=note)
        else:
            lot = create_stock_lot(line.batch_line, line.location, line.quantity)
            receive_stock_lot(lot, by=by, comment=note)

    receipt.batch = batch
    receipt.status = Receipt.Status.POSTED
    receipt.posted_by = by
    receipt.posted_at = timezone.now()
    receipt.save(update_fields=["batch", "status", "posted_by", "posted_at"])
    return receipt
