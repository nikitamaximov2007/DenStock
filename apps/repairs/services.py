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

from apps.customers.services import customer_snapshot
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


def _default_customer_price(part_type):
    """Current catalog price is only a draft default, never a historical fallback."""
    return part_type.recommended_price


# --- Создание / наполнение заказа --------------------------------------------


def create_repair_order(
    *,
    customer_name="",
    customer_phone="",
    vehicle_type=None,
    vehicle_make="",
    vehicle_model="",
    vehicle_identifier="",
    problem_description="",
    comment="",
    by=None,
    customer=None,
) -> RepairOrder:
    """Создать черновик ремонтного заказа (склад ещё не трогаем)."""
    # Выбрана карточка клиента - документ забирает её АКТУАЛЬНЫЕ имя и телефон
    # как снимок. Дальше документ от карточки не зависит: переименование
    # карточки завтра историю не переписывает.
    snapshot = customer_snapshot(
        customer, fallback_name=customer_name, fallback_phone=customer_phone
    )
    customer_name = snapshot["customer_name"]
    customer_phone = snapshot["customer_phone"]
    if not customer_name:
        raise RepairError("Не указан клиент.")
    return RepairOrder.objects.create(
        customer=customer,
        customer_name=customer_name,
        customer_phone=customer_phone,
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
def add_part_item_to_repair_order(
    order, item, *, note="", customer_unit_price_rub=None, by=None
) -> RepairIssueLine:
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
        repair_order=order,
        part_type=item.part_type,
        part_item=item,
        batch=item.batch,
        batch_line=item.batch_line,
        quantity=Decimal("1"),
        note=(note or "").strip(),
        customer_unit_price_rub=(
            _default_customer_price(item.part_type)
            if customer_unit_price_rub is None
            else Decimal(customer_unit_price_rub)
        ),
    )


@transaction.atomic
def add_stock_lot_to_repair_order(
    order, lot, quantity, *, note="", customer_unit_price_rub=None, by=None
) -> RepairIssueLine:
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
    already_in_order = RepairIssueLine.objects.filter(repair_order=order, stock_lot=lot).aggregate(
        s=Sum("quantity")
    )["s"] or Decimal("0")
    available = lot.quantity - reserved - already_in_order
    if quantity > available:
        raise RepairError(
            f"Недостаточно в лоте: доступно для выдачи {available}, запрошено {quantity}."
        )
    return RepairIssueLine.objects.create(
        repair_order=order,
        part_type=lot.part_type,
        stock_lot=lot,
        batch=lot.batch,
        batch_line=lot.batch_line,
        quantity=quantity,
        note=(note or "").strip(),
        customer_unit_price_rub=(
            _default_customer_price(lot.part_type)
            if customer_unit_price_rub is None
            else Decimal(customer_unit_price_rub)
        ),
    )


@transaction.atomic
def remove_repair_line(line, *, by=None) -> None:
    """Снять позицию из черновика заказа."""
    line = (
        RepairIssueLine.objects.select_for_update().select_related("repair_order").get(pk=line.pk)
    )
    _ensure_draft(line.repair_order)
    line.delete()


@transaction.atomic
def set_repair_line_customer_price(line, customer_unit_price_rub, *, by=None) -> RepairIssueLine:
    """Set the client price only while the repair is a draft."""
    line = (
        RepairIssueLine.objects.select_for_update().select_related("repair_order").get(pk=line.pk)
    )
    _ensure_draft(line.repair_order)
    if customer_unit_price_rub in (None, ""):
        line.customer_unit_price_rub = None
    else:
        price = Decimal(customer_unit_price_rub)
        if price < 0:
            raise RepairError("Цена клиента не может быть отрицательной.")
        line.customer_unit_price_rub = money(price)
    line.save(update_fields=["customer_unit_price_rub"])
    return line


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
            issue_part_item(item, by=by, document_id=order.pk, comment=f"Ремонт {order.number}")
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


def repair_customer_line_amounts(lines):
    """Net historical customer amount per issue line; ``None`` preserves unknown."""
    from apps.returns.models import StockReturnLine

    lines = list(lines)
    line_ids = [line.pk for line in lines]
    returned = dict(
        StockReturnLine.objects.filter(
            stock_return__status="completed",
            source_repair_line_id__in=line_ids,
        )
        .values("source_repair_line_id")
        .annotate(quantity=Sum("quantity"))
        .values_list("source_repair_line_id", "quantity")
    )
    amounts = {}
    for line in lines:
        remaining = max(line.quantity - (returned.get(line.pk) or Decimal("0")), Decimal("0"))
        amounts[line.pk] = (
            None
            if line.customer_unit_price_rub is None and remaining
            else money(line.customer_unit_price_rub * remaining)
            if line.customer_unit_price_rub is not None
            else Decimal("0")
        )
    return amounts


def repair_customer_amounts(orders):
    """Net historical customer amounts keyed by repair id.

    ``None`` means at least one issued line has no historical customer price.
    Completed returns reduce the amount using the same frozen issue-line price.
    """
    order_ids = [order.pk for order in orders]
    values = {pk: Decimal("0") for pk in order_ids}
    unknown = {pk: False for pk in order_ids}
    lines = list(
        RepairIssueLine.objects.filter(repair_order_id__in=order_ids).only(
            "id", "repair_order_id", "quantity", "customer_unit_price_rub"
        )
    )
    line_amounts = repair_customer_line_amounts(lines)
    for line in lines:
        line_amount = line_amounts[line.pk]
        if line_amount is None:
            unknown[line.repair_order_id] = True
        else:
            values[line.repair_order_id] += line_amount
    return {pk: None if unknown[pk] else money(values[pk]) for pk in order_ids}


def calculate_repair_customer_amount(order: RepairOrder):
    return repair_customer_amounts([order])[order.pk]
