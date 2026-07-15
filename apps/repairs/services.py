"""Слой 17 — сервисы выдачи деталей в ремонт / установку на технику.

Единственная точка изменения ремонтного заказа. `apps.repairs` ведёт ДОКУМЕНТ
(шапку, строки, заморозку себестоимости) и оркеструет проведение, но физику
склада НЕ трогает: списание (`PartItem.status`/`StockLot.quantity`,
`StockMovement`, `StockBalance`) делают сервисы `apps.inventory`
(`issue_part_item`/`issue_stock_lot`). View сюда только делегирует.

Резерв-осведомлённость берём из публичного API `apps.sales`
(`is_part_item_reserved`/`active_reserved_for_lot`): нельзя выдать в ремонт то,
что держит активная бронь. Связи repair-заказа с `Reservation` на этом слое нет.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.inventory.models import PartItem, StockLot
from apps.inventory.services import issue_part_item, issue_stock_lot
from apps.procurement.models import money
from apps.sales.services import active_reserved_for_lot, is_part_item_reserved

from .models import RepairIssueLine, RepairOrder


class RepairError(Exception):
    """Невозможно выполнить операцию с ремонтным заказом."""


# --- Заморозка себестоимости -------------------------------------------------


def _freeze_repair_line_cost(line: RepairIssueLine) -> None:
    """Заморозить себестоимость строки на момент выдачи (из landed cost объекта)."""
    if line.part_item_id:
        unit_cost = line.part_item.landed_cost_rub
    else:
        unit_cost = line.stock_lot.landed_unit_cost_rub
    line.unit_cost_rub = unit_cost
    line.total_cost_rub = money(unit_cost * line.quantity)


# --- Создание / наполнение заказа --------------------------------------------


def create_repair_order(
    *, customer_name, customer_phone="", vehicle_type=None, vehicle_make="",
    vehicle_model="", vehicle_identifier="", problem_description="", comment="", by=None,
) -> RepairOrder:
    """Создать черновик ремонтного заказа (склад ещё не трогаем)."""
    customer_name = (customer_name or "").strip()
    if not customer_name:
        raise RepairError("Не указан клиент.")
    return RepairOrder.objects.create(
        customer_name=customer_name,
        customer_phone=(customer_phone or "").strip(),
        vehicle_type=vehicle_type,
        vehicle_make=(vehicle_make or "").strip(),
        vehicle_model=(vehicle_model or "").strip(),
        vehicle_identifier=(vehicle_identifier or "").strip(),
        problem_description=(problem_description or "").strip(),
        comment=(comment or "").strip(),
        created_by=by,
        status=RepairOrder.Status.DRAFT,
    )


def _ensure_draft(order: RepairOrder) -> None:
    if order.status != RepairOrder.Status.DRAFT:
        raise RepairError("Заказ уже проведён или отменён — изменять состав нельзя.")


@transaction.atomic
def add_part_item_to_repair_order(order, item, *, note="", by=None) -> RepairIssueLine:
    """Добавить конкретный экземпляр в заказ (целиком, quantity = 1)."""
    order = RepairOrder.objects.select_for_update().get(pk=order.pk)
    _ensure_draft(order)
    item = PartItem.objects.select_for_update().get(pk=item.pk)
    if item.status != PartItem.Status.AVAILABLE:
        raise RepairError("Выдать в ремонт можно только доступный экземпляр.")
    if RepairIssueLine.objects.filter(repair_order=order, part_item=item).exists():
        raise RepairError("Этот экземпляр уже в этом заказе.")
    if is_part_item_reserved(item):
        raise RepairError("Экземпляр зарезервирован активной бронью.")
    return RepairIssueLine.objects.create(
        repair_order=order, part_type=item.part_type, part_item=item,
        batch=item.batch, batch_line=item.batch_line,
        quantity=Decimal("1"), note=(note or "").strip(),
    )


@transaction.atomic
def add_stock_lot_to_repair_order(order, lot, quantity, *, note="", by=None) -> RepairIssueLine:
    """Добавить количество из лота в заказ. Доступно = qty − резерв − уже в заказе."""
    order = RepairOrder.objects.select_for_update().get(pk=order.pk)
    _ensure_draft(order)
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise RepairError("Количество должно быть больше нуля.")
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status != StockLot.Status.AVAILABLE:
        raise RepairError("Выдать в ремонт можно только доступный лот.")
    reserved = active_reserved_for_lot(lot)
    already_in_order = (
        RepairIssueLine.objects.filter(repair_order=order, stock_lot=lot)
        .aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )
    available = lot.quantity - reserved - already_in_order
    if quantity > available:
        raise RepairError(
            f"Недостаточно в лоте: доступно для выдачи {available}, запрошено {quantity}."
        )
    return RepairIssueLine.objects.create(
        repair_order=order, part_type=lot.part_type, stock_lot=lot,
        batch=lot.batch, batch_line=lot.batch_line,
        quantity=quantity, note=(note or "").strip(),
    )


@transaction.atomic
def remove_repair_line(line, *, by=None) -> None:
    """Снять позицию из черновика заказа."""
    line = (
        RepairIssueLine.objects.select_for_update().select_related("repair_order").get(pk=line.pk)
    )
    _ensure_draft(line.repair_order)
    line.delete()


# --- Проведение / отмена -----------------------------------------------------


@transaction.atomic
def complete_repair_order(order, *, by=None) -> RepairOrder:
    """Провести заказ: выдать остаток через inventory.issue_*, заморозить
    себестоимость. На проведении заново проверяем доступность каждой строки —
    если деталь успели продать/зарезервировать/выдать, падаем с понятной ошибкой.
    """
    order = RepairOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == RepairOrder.Status.COMPLETED:
        return order
    if order.status != RepairOrder.Status.DRAFT:
        raise RepairError("Заказ уже проведён или отменён.")
    lines = list(order.lines.select_related("part_item", "stock_lot", "part_type"))
    if not lines:
        raise RepairError("Нельзя провести пустой заказ.")

    now = timezone.now()
    for line in lines:
        if line.part_item_id:
            item = PartItem.objects.select_for_update().get(pk=line.part_item_id)
            if item.status != PartItem.Status.AVAILABLE:
                raise RepairError(f"Экземпляр {item.internal_number} недоступен.")
            if is_part_item_reserved(item):
                raise RepairError(
                    f"Экземпляр {item.internal_number} зарезервирован активной бронью."
                )
            line.part_item = item
            _freeze_repair_line_cost(line)
            line.issued_at = now
            line.save(update_fields=["unit_cost_rub", "total_cost_rub", "issued_at"])
            issue_part_item(
                item, by=by, document_id=order.pk, comment=f"Ремонт {order.number}"
            )
        else:
            lot = StockLot.objects.select_for_update().get(pk=line.stock_lot_id)
            if lot.status != StockLot.Status.AVAILABLE:
                raise RepairError(f"Лот #{lot.pk} недоступен.")
            reserved = active_reserved_for_lot(lot)
            if line.quantity > lot.quantity - reserved:
                raise RepairError(
                    f"Лот #{lot.pk}: доступно для выдачи {lot.quantity - reserved}, "
                    f"нужно {line.quantity}."
                )
            line.stock_lot = lot
            _freeze_repair_line_cost(line)
            line.issued_at = now
            line.save(update_fields=["unit_cost_rub", "total_cost_rub", "issued_at"])
            issue_stock_lot(
                lot, line.quantity, by=by, document_id=order.pk, comment=f"Ремонт {order.number}"
            )

    order.cost_total = calculate_repair_costs(order)
    order.status = RepairOrder.Status.COMPLETED
    order.completed_at = now
    order.save(update_fields=["cost_total", "status", "completed_at", "updated_at"])
    return order


@transaction.atomic
def cancel_repair_order(order, *, by=None) -> RepairOrder:
    """Отменить черновик заказа (склад не затрагивали — отменять можно только draft)."""
    order = RepairOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == RepairOrder.Status.CANCELED:
        return order
    if order.status != RepairOrder.Status.DRAFT:
        raise RepairError("Отменить можно только черновик (проведённый заказ неизменяем).")
    order.status = RepairOrder.Status.CANCELED
    order.canceled_at = timezone.now()
    order.save(update_fields=["status", "canceled_at", "updated_at"])
    return order


def calculate_repair_costs(order: RepairOrder) -> Decimal:
    """Стоимость фактически использованных деталей с учётом возвратов на склад."""
    from apps.returns.models import StockReturn, StockReturnLine

    returned_by_line = dict(
        StockReturnLine.objects.filter(
            stock_return__status=StockReturn.Status.COMPLETED,
            source_repair_line__repair_order=order,
        )
        .values("source_repair_line_id")
        .annotate(quantity=Sum("quantity"))
        .values_list("source_repair_line_id", "quantity")
    )
    total = Decimal("0")
    for line in order.lines.all():
        returned = returned_by_line.get(line.pk) or Decimal("0")
        remaining_used = max(line.quantity - returned, Decimal("0"))
        total += line.unit_cost_rub * remaining_used
    return money(total)
