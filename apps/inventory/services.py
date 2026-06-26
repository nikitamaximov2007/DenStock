"""Слой 8 — создание поштучных экземпляров (PartItem).

Создаёт физические экземпляры из строки уже финансово закрытой партии. Без
складских движений (`StockMovement`/`StockBalance` — Слой 10) и без сканера.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce

from apps.procurement.models import BatchLine
from apps.warehouse.models import StorageLocation

from .models import NumberSequence, PartItem, StockBalance, StockLot, StockMovement


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


# --- Слой 10: журнал движений (StockMovement) и кэш остатков (StockBalance) ---

DEC0 = Value(Decimal("0"), output_field=DecimalField(max_digits=12, decimal_places=3))

# Статусы, считающиеся «физически присутствует в ячейке» (Слой 10).
# reserved/repair добавятся, когда появятся их производители (Слои 15/17).
ITEM_PHYSICAL_STATUSES = (
    PartItem.Status.RECEIVING,
    PartItem.Status.AVAILABLE,
    PartItem.Status.QUARANTINE,
)
LOT_PHYSICAL_STATUSES = (
    StockLot.Status.RECEIVING,
    StockLot.Status.AVAILABLE,
    StockLot.Status.QUARANTINE,
)

# --- Слой 15: провайдер «зарезервировано» (инверсия зависимости) -------------
# inventory НЕ импортирует apps.sales. Приложение sales в AppConfig.ready()
# регистрирует функцию reserved_for(batch_line, location) -> Decimal через
# set_reserved_provider. До регистрации (или если sales не установлен) резерв = 0,
# и поведение инвентаря не меняется. Хук вызывается при КАЖДОМ пересчёте строки
# кэша, поэтому move_*/receive_*/adjust_*/rebuild не затирают quantity_reserved.
_reserved_provider = None


def set_reserved_provider(provider) -> None:
    """Зарегистрировать callable(batch_line, location) -> Decimal."""
    global _reserved_provider
    _reserved_provider = provider


def _reserved_for(batch_line, location) -> Decimal:
    if _reserved_provider is None:
        return Decimal("0")
    return _reserved_provider(batch_line, location)


def _record_movement(
    obj,
    movement_type,
    quantity,
    *,
    from_location=None,
    to_location=None,
    by=None,
    comment="",
    document_type="",
    document_id=None,
) -> StockMovement:
    """Единая точка создания движения: источник копируется из объекта.

    `total_cost_rub` считается в `StockMovement.save()` (= unit_cost × quantity).
    `document_type`/`document_id` связывают движение с документом-источником
    (например, продажей: `document_type="sale"`, `document_id=Sale.id`).
    """
    if isinstance(obj, PartItem):
        part_item, stock_lot = obj, None
        unit_cost = obj.landed_cost_rub
    else:
        part_item, stock_lot = None, obj
        unit_cost = obj.landed_unit_cost_rub
    return StockMovement.objects.create(
        movement_type=movement_type,
        part_type=obj.part_type,
        part_item=part_item,
        stock_lot=stock_lot,
        batch=obj.batch,
        batch_line=obj.batch_line,
        from_location=from_location,
        to_location=to_location,
        quantity=Decimal(quantity),
        unit_cost_rub=unit_cost,
        created_by=by,
        comment=comment,
        document_type=document_type,
        document_id=document_id,
    )


def _compute_balance(batch_line, location) -> dict | None:
    """Эталонные количества для (строка партии × ячейка) из первички.

    Возвращает None, если физического остатка нет (строки кэша быть не должно).
    """
    part = batch_line.part_type
    if part.tracking_mode == part.TrackingMode.BULK:
        agg = StockLot.objects.filter(
            batch_line=batch_line, location=location, status__in=LOT_PHYSICAL_STATUSES,
        ).aggregate(
            physical=Coalesce(Sum("quantity"), DEC0),
            quarantine=Coalesce(
                Sum("quantity", filter=Q(status=StockLot.Status.QUARANTINE)), DEC0
            ),
        )
        physical, quarantine = agg["physical"], agg["quarantine"]
    else:
        base = PartItem.objects.filter(batch_line=batch_line, current_location=location)
        physical = Decimal(base.filter(status__in=ITEM_PHYSICAL_STATUSES).count())
        quarantine = Decimal(base.filter(status=PartItem.Status.QUARANTINE).count())
    if physical == 0:
        return None
    # Активный резерв приходит из apps.sales через провайдер (Слой 15); reserved
    # уменьшает доступность, но не физический остаток. Источник истины по резерву
    # — ReservationLine, здесь это лишь кэш.
    reserved = _reserved_for(batch_line, location)
    return {
        "part_type": part,
        "batch": batch_line.batch,
        "physical": physical,
        "quarantine": quarantine,
        "reserved": reserved,
        "available": physical - quarantine - reserved,
    }


def _refresh_balance(batch_line, location) -> str:
    """Освежить одну строку кэша из первички (или удалить, если остатка нет)."""
    data = _compute_balance(batch_line, location)
    if data is None:
        deleted, _ = StockBalance.objects.filter(
            batch_line=batch_line, location=location
        ).delete()
        return "deleted" if deleted else "noop"
    _, created = StockBalance.objects.update_or_create(
        batch_line=batch_line,
        location=location,
        defaults={
            "part_type": data["part_type"],
            "batch": data["batch"],
            "quantity_physical": data["physical"],
            "quantity_available": data["available"],
            "quantity_quarantine": data["quarantine"],
            "quantity_reserved": data["reserved"],
            "quantity_in_repair": Decimal("0"),
        },
    )
    return "created" if created else "updated"


def recompute_balance_row(batch_line, location) -> str:
    """Публичная точка пересчёта одной строки кэша из первички.

    Используется apps.sales после изменения брони (резерв сам ledger не трогает —
    ни `StockMovement`, ни прямой записи в `StockBalance`). Вызывается внутри
    транзакции сервиса-инициатора.
    """
    return _refresh_balance(batch_line, location)


@transaction.atomic
def receive_part_item(item: PartItem, *, to_location=None, by=None, comment="") -> PartItem:
    """Провести экземпляр в ячейку: receiving → available, записать движение."""
    item = PartItem.objects.select_for_update().get(pk=item.pk)
    if item.status != PartItem.Status.RECEIVING:
        raise InventoryError("Экземпляр уже проведён (не на приёмке).")
    location = to_location or item.current_location
    if location is None:
        raise InventoryError("Не указана ячейка приёмки.")
    if not location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")
    item.current_location = location
    item.status = PartItem.Status.AVAILABLE
    item.save(update_fields=["current_location", "status", "updated_at"])
    _record_movement(
        item, StockMovement.MovementType.RECEIVE_ITEM, Decimal("1"),
        to_location=location, by=by, comment=comment,
    )
    _refresh_balance(item.batch_line, location)
    return item


@transaction.atomic
def receive_stock_lot(lot: StockLot, *, by=None, comment="") -> StockLot:
    """Провести лот: receiving → available, записать движение (qty = lot.quantity)."""
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status != StockLot.Status.RECEIVING:
        raise InventoryError("Лот уже проведён (не на приёмке).")
    if not lot.location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")
    lot.status = StockLot.Status.AVAILABLE
    lot.save(update_fields=["status", "updated_at"])
    _record_movement(
        lot, StockMovement.MovementType.RECEIVE_LOT, lot.quantity,
        to_location=lot.location, by=by, comment=comment,
    )
    _refresh_balance(lot.batch_line, lot.location)
    return lot


@transaction.atomic
def move_part_item(item: PartItem, to_location, *, by=None, comment="") -> PartItem:
    """Перенести экземпляр в другую ячейку (статус не меняется)."""
    if to_location is None or not to_location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")
    item = PartItem.objects.select_for_update().get(pk=item.pk)
    if item.status not in ITEM_PHYSICAL_STATUSES:
        raise InventoryError("Экземпляр в недопустимом статусе для перемещения.")
    from_location = item.current_location
    if from_location is None:
        raise InventoryError("Экземпляр не размещён — сначала проведите приёмку.")
    if from_location.pk == to_location.pk:
        raise InventoryError("Экземпляр уже в этой ячейке.")
    item.current_location = to_location
    item.save(update_fields=["current_location", "updated_at"])
    _record_movement(
        item, StockMovement.MovementType.MOVE_ITEM, Decimal("1"),
        from_location=from_location, to_location=to_location, by=by, comment=comment,
    )
    _refresh_balance(item.batch_line, from_location)
    _refresh_balance(item.batch_line, to_location)
    return item


@transaction.atomic
def move_stock_lot(lot: StockLot, to_location, *, by=None, comment="") -> StockLot:
    """Перенести лот целиком (частичное перемещение/слияние — будущий слой)."""
    if to_location is None or not to_location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status not in LOT_PHYSICAL_STATUSES:
        raise InventoryError("Лот в недопустимом статусе для перемещения.")
    from_location = lot.location
    if from_location.pk == to_location.pk:
        raise InventoryError("Лот уже в этой ячейке.")
    clash = (
        StockLot.objects.filter(batch_line=lot.batch_line, location=to_location)
        .exclude(pk=lot.pk)
        .exists()
    )
    if clash:
        raise InventoryError(
            "В этой ячейке уже есть лот этой строки; слияние лотов — будущий слой."
        )
    lot.location = to_location
    lot.save(update_fields=["location", "updated_at"])
    _record_movement(
        lot, StockMovement.MovementType.MOVE_LOT, lot.quantity,
        from_location=from_location, to_location=to_location, by=by, comment=comment,
    )
    _refresh_balance(lot.batch_line, from_location)
    _refresh_balance(lot.batch_line, to_location)
    return lot


@transaction.atomic
def adjust_stock_lot_quantity(lot: StockLot, delta, *, by=None, comment="") -> StockLot:
    """Скорректировать количество лота на delta (±). При нуле — статус depleted."""
    delta = Decimal(delta)
    if delta == 0:
        raise InventoryError("Изменение количества не может быть нулевым.")
    if not comment or not comment.strip():
        raise InventoryError("Для корректировки обязателен комментарий.")
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status not in LOT_PHYSICAL_STATUSES:
        raise InventoryError("Лот в недопустимом статусе для корректировки.")
    new_qty = lot.quantity + delta
    if new_qty < 0:
        raise InventoryError(
            f"Корректировка уводит количество в минус: остаток {lot.quantity}, дельта {delta}."
        )
    if delta > 0:
        movement_type = StockMovement.MovementType.ADJUST_IN
        from_location, to_location = None, lot.location
    else:
        movement_type = StockMovement.MovementType.ADJUST_OUT
        from_location, to_location = lot.location, None
    lot.quantity = new_qty
    if new_qty == 0:
        lot.status = StockLot.Status.DEPLETED
    lot.save(update_fields=["quantity", "status", "updated_at"])
    _record_movement(
        lot, movement_type, abs(delta),
        from_location=from_location, to_location=to_location, by=by, comment=comment,
    )
    _refresh_balance(lot.batch_line, lot.location)
    return lot


# --- Расход со склада: общий механизм для продажи (Слой 16) и выдачи в --------
# --- ремонт (Слой 17). Физика + ledger, без знания о резервах. ----------------
#
# Продажа и выдача в ремонт — это один и тот же складской расход (экземпляр
# уходит из available, лот уменьшается, пишется расходное движение), различаются
# лишь целевым статусом, типом движения и документом-источником. Поэтому общая
# механика вынесена в приватные `_consume_*`, а `sell_*`/`issue_*` — тонкие
# обёртки. Резерв-проверки делает вызывающий слой (apps.sales/apps.repairs) ДО
# вызова — здесь только физический статус.


@transaction.atomic
def _consume_part_item(
    item, *, new_status, movement_type, document_type, unavailable_msg,
    by=None, document_id=None, comment="",
) -> PartItem:
    """Списать экземпляр со склада: available → new_status, расходное движение.

    `current_location` сохраняется как последняя известная ячейка (аудит); любой
    из терминальных статусов (`sold`/`installed`) исключает экземпляр из
    physical/available.
    """
    item = PartItem.objects.select_for_update().get(pk=item.pk)
    if item.status != PartItem.Status.AVAILABLE:
        raise InventoryError(unavailable_msg)
    from_location = item.current_location
    item.status = new_status
    item.save(update_fields=["status", "updated_at"])
    _record_movement(
        item, movement_type, Decimal("1"),
        from_location=from_location, to_location=None, by=by,
        comment=comment, document_type=document_type, document_id=document_id,
    )
    if from_location is not None:
        _refresh_balance(item.batch_line, from_location)
    return item


@transaction.atomic
def _consume_stock_lot(
    lot, quantity, *, movement_type, document_type,
    positive_msg, unavailable_msg, over_msg,
    by=None, document_id=None, comment="",
) -> StockLot:
    """Списать количество из лота: quantity↓, depleted при нуле, расходное
    движение. Частичный расход разрешён; лот не дробится."""
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise InventoryError(positive_msg)
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status != StockLot.Status.AVAILABLE:
        raise InventoryError(unavailable_msg)
    if quantity > lot.quantity:
        raise InventoryError(over_msg.format(quantity=quantity, in_lot=lot.quantity))
    lot.quantity = lot.quantity - quantity
    if lot.quantity == 0:
        lot.status = StockLot.Status.DEPLETED
    lot.save(update_fields=["quantity", "status", "updated_at"])
    _record_movement(
        lot, movement_type, quantity,
        from_location=lot.location, to_location=None, by=by,
        comment=comment, document_type=document_type, document_id=document_id,
    )
    _refresh_balance(lot.batch_line, lot.location)
    return lot


# --- Слой 16: расход при продаже ---------------------------------------------


def sell_part_item(item, *, by=None, document_id=None, comment="") -> PartItem:
    """Списать экземпляр при продаже: available → sold, движение SALE_ITEM."""
    return _consume_part_item(
        item, new_status=PartItem.Status.SOLD,
        movement_type=StockMovement.MovementType.SALE_ITEM, document_type="sale",
        unavailable_msg="Продать можно только доступный экземпляр.",
        by=by, document_id=document_id, comment=comment,
    )


def sell_stock_lot(lot, quantity, *, by=None, document_id=None, comment="") -> StockLot:
    """Списать количество из лота при продаже: quantity↓, depleted при нуле,
    движение SALE_LOT."""
    return _consume_stock_lot(
        lot, quantity, movement_type=StockMovement.MovementType.SALE_LOT,
        document_type="sale",
        positive_msg="Количество продажи должно быть больше нуля.",
        unavailable_msg="Продать можно только доступный лот.",
        over_msg="Нельзя продать {quantity}: в лоте {in_lot}.",
        by=by, document_id=document_id, comment=comment,
    )


# --- Слой 17: расход при выдаче в ремонт / установке --------------------------
#
# Выдача в ремонт — окончательный складской расход (не временная передача
# мастеру): экземпляр становится `installed`, лот уменьшается. Источник-документ
# — RepairOrder (`document_type="repair_order"`). Сервисы не знают о ремонтном
# приложении и о резервах (проверки делает apps.repairs до вызова).


def issue_part_item(item, *, by=None, document_id=None, comment="") -> PartItem:
    """Выдать экземпляр в ремонт: available → installed, движение ISSUE_ITEM."""
    return _consume_part_item(
        item, new_status=PartItem.Status.INSTALLED,
        movement_type=StockMovement.MovementType.ISSUE_ITEM,
        document_type="repair_order",
        unavailable_msg="Выдать можно только доступный экземпляр.",
        by=by, document_id=document_id, comment=comment,
    )


def issue_stock_lot(lot, quantity, *, by=None, document_id=None, comment="") -> StockLot:
    """Выдать количество из лота в ремонт: quantity↓, depleted при нуле,
    движение ISSUE_LOT. Частичная выдача разрешена; лот не дробится."""
    return _consume_stock_lot(
        lot, quantity, movement_type=StockMovement.MovementType.ISSUE_LOT,
        document_type="repair_order",
        positive_msg="Количество выдачи должно быть больше нуля.",
        unavailable_msg="Выдать можно только доступный лот.",
        over_msg="Нельзя выдать {quantity}: в лоте {in_lot}.",
        by=by, document_id=document_id, comment=comment,
    )


# --- Слой 18: возврат на склад (физическое обратное поступление) --------------
#
# Инверс расхода Слоёв 16/17: деталь возвращается на полку (PartItem снова
# физический / StockLot.quantity растёт), пишется приходное движение RETURN_*.
# Сервисы источник-агностичны: им передают готовые to_location/restock_status/
# unit_cost — бизнес-проверки (источник, «не больше проданного») делает
# apps.returns ДО вызова. Это НЕ денежный refund — только физика склада.

_RESTOCK_STATUSES = (PartItem.Status.AVAILABLE, PartItem.Status.QUARANTINE)


@transaction.atomic
def return_part_item(item, to_location, *, restock_status, by=None,
                     document_id=None, comment="") -> PartItem:
    """Вернуть экземпляр на склад: sold/installed → restock_status (available/
    quarantine), движение RETURN_ITEM. `current_location` ставится в ячейку возврата.
    """
    if restock_status not in _RESTOCK_STATUSES:
        raise InventoryError("Недопустимое состояние возврата (ожидается available/quarantine).")
    if to_location is None or not to_location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")
    item = PartItem.objects.select_for_update().get(pk=item.pk)
    if item.status not in (PartItem.Status.SOLD, PartItem.Status.INSTALLED):
        raise InventoryError("Вернуть можно только проданный или выданный экземпляр.")
    item.status = restock_status
    item.current_location = to_location
    item.save(update_fields=["status", "current_location", "updated_at"])
    _record_movement(
        item, StockMovement.MovementType.RETURN_ITEM, Decimal("1"),
        from_location=None, to_location=to_location, by=by,
        comment=comment, document_type="stock_return", document_id=document_id,
    )
    _refresh_balance(item.batch_line, to_location)
    return item


@transaction.atomic
def return_stock_lot_quantity(batch_line, to_location, quantity, *, unit_cost_rub,
                              restock_status, by=None, document_id=None, comment="") -> StockLot:
    """Вернуть количество в лот ячейки `to_location` по правилу «найти/оживить/
    создать» под UniqueConstraint(batch_line, location):

    - нет лота в ячейке → создаём новый;
    - лот `depleted` → оживляем (quantity += возврат, status → restock_status);
    - лот того же физического статуса → доливаем;
    - лот другого физического/терминального статуса → ошибка (без смешивания).

    Движение RETURN_LOT. Лот не дробим.
    """
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise InventoryError("Количество возврата должно быть больше нуля.")
    if restock_status not in (StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE):
        raise InventoryError("Недопустимое состояние возврата (ожидается available/quarantine).")
    if to_location is None or not to_location.can_hold_stock():
        raise InventoryError("Это место не предназначено для хранения остатка.")
    lot = (
        StockLot.objects.select_for_update()
        .filter(batch_line=batch_line, location=to_location)
        .first()
    )
    if lot is None:
        lot = StockLot.objects.create(
            part_type=batch_line.part_type, batch=batch_line.batch, batch_line=batch_line,
            location=to_location, quantity=quantity, initial_quantity=quantity,
            landed_unit_cost_rub=unit_cost_rub, status=restock_status,
        )
    else:
        if lot.status == StockLot.Status.DEPLETED or lot.status == restock_status:
            lot.quantity = lot.quantity + quantity
            lot.status = restock_status
            lot.save(update_fields=["quantity", "status", "updated_at"])
        elif lot.status in LOT_PHYSICAL_STATUSES:
            raise InventoryError(
                f"В ячейке {to_location.code} уже есть лот этой строки в статусе "
                f"«{lot.get_status_display()}»; выберите другую ячейку или согласуйте статус."
            )
        else:
            raise InventoryError(
                f"В ячейке {to_location.code} лот этой строки в статусе "
                f"«{lot.get_status_display()}» — возврат недоступен."
            )
    _record_movement(
        lot, StockMovement.MovementType.RETURN_LOT, quantity,
        from_location=None, to_location=to_location, by=by,
        comment=comment, document_type="stock_return", document_id=document_id,
    )
    _refresh_balance(batch_line, to_location)
    return lot


# --- Пересборка и сверка кэша остатков ---------------------------------------


def _primary_pairs() -> set[tuple[int, int]]:
    """Пары (batch_line_id, location_id) c физической первичкой."""
    pairs: set[tuple[int, int]] = set()
    pairs.update(
        StockLot.objects.filter(status__in=LOT_PHYSICAL_STATUSES).values_list(
            "batch_line_id", "location_id"
        )
    )
    pairs.update(
        PartItem.objects.filter(
            status__in=ITEM_PHYSICAL_STATUSES, current_location__isnull=False
        ).values_list("batch_line_id", "current_location_id")
    )
    return pairs


def _line_loc_maps(keys):
    """Кэш объектов BatchLine/StorageLocation по id для набора пар."""
    lines = {
        bl.pk: bl
        for bl in BatchLine.objects.filter(pk__in={k[0] for k in keys}).select_related(
            "part_type", "batch"
        )
    }
    locations = {
        loc.pk: loc for loc in StorageLocation.objects.filter(pk__in={k[1] for k in keys})
    }
    return lines, locations


@transaction.atomic
def rebuild_stock_balance() -> dict:
    """Полностью пересобрать кэш остатков из первички. Возвращает счётчики."""
    counts = {"created": 0, "updated": 0, "deleted": 0}
    pairs = _primary_pairs()
    existing = set(StockBalance.objects.values_list("batch_line_id", "location_id"))
    for bl_id, loc_id in existing - pairs:
        deleted, _ = StockBalance.objects.filter(
            batch_line_id=bl_id, location_id=loc_id
        ).delete()
        if deleted:
            counts["deleted"] += 1
    if not pairs:
        return counts
    lines, locations = _line_loc_maps(pairs)
    for bl_id, loc_id in pairs:
        action = _refresh_balance(lines[bl_id], locations[loc_id])
        if action in counts:
            counts[action] += 1
    return counts


def check_stock_balance() -> list[str]:
    """Сверить кэш с первичкой. Возвращает список расхождений (без правок)."""
    problems: list[str] = []
    pairs = _primary_pairs()
    existing = {
        (b.batch_line_id, b.location_id): b
        for b in StockBalance.objects.select_related("part_type", "location")
    }
    keys = pairs | set(existing.keys())
    if not keys:
        return problems
    lines, locations = _line_loc_maps(keys)
    for bl_id, loc_id in sorted(keys):
        ideal = _compute_balance(lines[bl_id], locations[loc_id])
        actual = existing.get((bl_id, loc_id))
        label = f"{lines[bl_id]} @ {locations[loc_id].code}"
        if ideal is None:
            if actual is not None:
                problems.append(f"{label}: лишняя строка кэша (физического остатка нет)")
            continue
        if actual is None:
            problems.append(f"{label}: нет строки кэша (ожидается physical={ideal['physical']})")
            continue
        if (
            actual.quantity_physical != ideal["physical"]
            or actual.quantity_available != ideal["available"]
            or actual.quantity_quarantine != ideal["quarantine"]
            or actual.quantity_reserved != ideal["reserved"]
        ):
            problems.append(
                f"{label}: кэш physical={actual.quantity_physical}/"
                f"avail={actual.quantity_available}/quar={actual.quantity_quarantine}/"
                f"resv={actual.quantity_reserved}, "
                f"эталон physical={ideal['physical']}/avail={ideal['available']}/"
                f"quar={ideal['quarantine']}/resv={ideal['reserved']}"
            )
    return problems


@transaction.atomic
def backfill_opening_movements(*, by=None) -> int:
    """Создать открывающее движение для первички без движений. Идемпотентна.

    Не меняет статусы/количества — только пишет историю. Корректный баланс
    обеспечивает `rebuild_stock_balance`, эта команда необязательна.
    """
    created = 0
    items = PartItem.objects.filter(
        status__in=ITEM_PHYSICAL_STATUSES,
        current_location__isnull=False,
        movements__isnull=True,
    ).select_related("part_type", "batch", "batch_line", "current_location")
    for item in items:
        _record_movement(
            item, StockMovement.MovementType.RECEIVE_ITEM, Decimal("1"),
            to_location=item.current_location, by=by, comment="Открывающий остаток",
        )
        created += 1
    lots = StockLot.objects.filter(
        status__in=LOT_PHYSICAL_STATUSES, movements__isnull=True
    ).select_related("part_type", "batch", "batch_line", "location")
    for lot in lots:
        _record_movement(
            lot, StockMovement.MovementType.RECEIVE_LOT, lot.quantity,
            to_location=lot.location, by=by, comment="Открывающий остаток",
        )
        created += 1
    return created

