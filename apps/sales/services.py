"""Слой 15 — сервисы резервов (коммерческая бронь).

Единственная точка изменения брони. Все мутации в `transaction.atomic` с
`select_for_update` на `PartItem`/`StockLot`. Бронь:

  - НЕ создаёт `StockMovement` (физического движения нет);
  - НЕ меняет `lot.quantity` / `PartItem.status` (source of truth — `ReservationLine`);
  - только активная бронь уменьшает доступность; пересчёт кэша `StockBalance`
    идёт через `inventory.recompute_balance_row` (inventory не импортирует sales).
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.inventory.models import PartItem, StockLot
from apps.inventory.services import recompute_balance_row
from apps.procurement.models import BatchLine
from apps.warehouse.models import StorageLocation

from .models import Reservation, ReservationLine


class ReservationError(Exception):
    """Невозможно выполнить операцию с резервом."""


# --- Активность брони и провайдер «зарезервировано» --------------------------


def _active_q(prefix: str = "reservation__") -> Q:
    """Q-фильтр «бронь активна и не истекла» (now вычисляется при каждом вызове)."""
    now = timezone.now()
    return Q(**{f"{prefix}status": Reservation.Status.ACTIVE}) & (
        Q(**{f"{prefix}expires_at__isnull": True}) | Q(**{f"{prefix}expires_at__gt": now})
    )


def reserved_for(batch_line, location) -> Decimal:
    """Активный зарезервированный остаток по (строка партии × ячейка).

    Чистая read-функция — её регистрирует `apps.sales.apps.ready()` как провайдер
    inventory. Учитываем только активные брони, объект которых ещё физически
    доступен (`available`): если экземпляр/лот вышел из `available`, он больше не
    держит доступность (и не уводит `available` в минус).
    """
    serial = (
        ReservationLine.objects.filter(
            _active_q(),
            part_item__status=PartItem.Status.AVAILABLE,
            part_item__batch_line=batch_line,
            part_item__current_location=location,
        ).aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )
    bulk = (
        ReservationLine.objects.filter(
            _active_q(),
            stock_lot__status=StockLot.Status.AVAILABLE,
            stock_lot__batch_line=batch_line,
            stock_lot__location=location,
        ).aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )
    return serial + bulk


def _active_reserved_for_lot(lot) -> Decimal:
    """Сколько количества лота держат активные брони (по всем резервам)."""
    return (
        ReservationLine.objects.filter(_active_q(), stock_lot=lot)
        .aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )


def _item_actively_reserved(item, *, exclude=None) -> bool:
    """Есть ли активная бронь на этот экземпляр (опц. исключая один резерв)."""
    qs = ReservationLine.objects.filter(_active_q(), part_item=item)
    if exclude is not None:
        qs = qs.exclude(reservation=exclude)
    return qs.exists()


# --- Пересчёт затронутых строк кэша ------------------------------------------


def _line_pair(line: ReservationLine):
    """(batch_line_id, location_id) для строки брони, либо None если нет ячейки."""
    if line.part_item_id:
        item = line.part_item
        if item.current_location_id is None:
            return None
        return item.batch_line_id, item.current_location_id
    lot = line.stock_lot
    return lot.batch_line_id, lot.location_id


def _recompute_pairs(pairs) -> None:
    for bl_id, loc_id in pairs:
        recompute_balance_row(
            BatchLine.objects.get(pk=bl_id), StorageLocation.objects.get(pk=loc_id)
        )


def _recompute_for_lines(lines) -> None:
    pairs = {p for p in (_line_pair(line) for line in lines) if p is not None}
    _recompute_pairs(pairs)


# --- Сервисы изменения брони -------------------------------------------------


def create_reservation(
    *, customer_name, customer_phone="", comment="", expires_at=None, by=None
) -> Reservation:
    """Создать черновик брони (остаток ещё не держим)."""
    customer_name = (customer_name or "").strip()
    if not customer_name:
        raise ReservationError("Не указан клиент.")
    return Reservation.objects.create(
        customer_name=customer_name,
        customer_phone=(customer_phone or "").strip(),
        comment=(comment or "").strip(),
        expires_at=expires_at,
        created_by=by,
        status=Reservation.Status.DRAFT,
    )


def _ensure_open(reservation: Reservation) -> None:
    if reservation.status not in (Reservation.Status.DRAFT, Reservation.Status.ACTIVE):
        raise ReservationError("Резерв закрыт — изменять состав нельзя.")


@transaction.atomic
def add_part_item_to_reservation(reservation, item, *, by=None) -> ReservationLine:
    """Добавить конкретный экземпляр в бронь (целиком, quantity = 1)."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    _ensure_open(reservation)
    item = PartItem.objects.select_for_update().get(pk=item.pk)
    if item.status != PartItem.Status.AVAILABLE:
        raise ReservationError("Зарезервировать можно только доступный экземпляр.")
    if ReservationLine.objects.filter(reservation=reservation, part_item=item).exists():
        raise ReservationError("Этот экземпляр уже в этом резерве.")
    if _item_actively_reserved(item, exclude=reservation):
        raise ReservationError("Экземпляр уже зарезервирован активной бронью.")
    line = ReservationLine.objects.create(
        reservation=reservation, part_type=item.part_type,
        part_item=item, quantity=Decimal("1"),
    )
    if reservation.status == Reservation.Status.ACTIVE and item.current_location_id:
        recompute_balance_row(item.batch_line, item.current_location)
    return line


@transaction.atomic
def add_stock_lot_to_reservation(reservation, lot, quantity, *, by=None) -> ReservationLine:
    """Зарезервировать количество из лота (частично/целиком), без дробления лота."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    _ensure_open(reservation)
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise ReservationError("Количество должно быть больше нуля.")
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status != StockLot.Status.AVAILABLE:
        raise ReservationError("Зарезервировать можно только доступный лот.")
    committed = _active_reserved_for_lot(lot)
    if reservation.status == Reservation.Status.DRAFT:
        # Активные брони не включают черновик — добавим уже намеченное в нём.
        committed += (
            ReservationLine.objects.filter(reservation=reservation, stock_lot=lot)
            .aggregate(s=Sum("quantity"))["s"]
            or Decimal("0")
        )
    if committed + quantity > lot.quantity:
        available = lot.quantity - committed
        raise ReservationError(
            f"Недостаточно в лоте: доступно для резерва {available}, запрошено {quantity}."
        )
    line = ReservationLine.objects.create(
        reservation=reservation, part_type=lot.part_type,
        stock_lot=lot, quantity=quantity,
    )
    if reservation.status == Reservation.Status.ACTIVE:
        recompute_balance_row(lot.batch_line, lot.location)
    return line


@transaction.atomic
def remove_reservation_line(line, *, by=None) -> None:
    """Снять позицию из брони (черновик/активная); активная — освобождает остаток."""
    line = (
        ReservationLine.objects.select_for_update()
        .select_related("reservation", "part_item", "stock_lot")
        .get(pk=line.pk)
    )
    reservation = line.reservation
    _ensure_open(reservation)
    was_active = reservation.status == Reservation.Status.ACTIVE
    pair = _line_pair(line)
    line.delete()
    if was_active and pair is not None:
        _recompute_pairs([pair])


@transaction.atomic
def activate_reservation(reservation, *, by=None) -> Reservation:
    """Перевести черновик в active: держит остаток. Нельзя активировать пустой."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status == Reservation.Status.ACTIVE:
        return reservation
    if reservation.status != Reservation.Status.DRAFT:
        raise ReservationError("Активировать можно только черновик.")
    lines = list(reservation.lines.select_related("part_item", "stock_lot"))
    if not lines:
        raise ReservationError("Нельзя активировать пустой резерв.")

    # Блокируем и проверяем каждый объект; суммируем количество по лотам.
    lot_demand: dict[int, Decimal] = {}
    for line in lines:
        if line.part_item_id:
            item = PartItem.objects.select_for_update().get(pk=line.part_item_id)
            if item.status != PartItem.Status.AVAILABLE:
                raise ReservationError(f"Экземпляр {item.internal_number} недоступен.")
            if _item_actively_reserved(item, exclude=reservation):
                raise ReservationError(
                    f"Экземпляр {item.internal_number} уже зарезервирован."
                )
        else:
            lot = StockLot.objects.select_for_update().get(pk=line.stock_lot_id)
            if lot.status != StockLot.Status.AVAILABLE:
                raise ReservationError(f"Лот #{lot.pk} недоступен.")
            lot_demand[lot.pk] = lot_demand.get(lot.pk, Decimal("0")) + line.quantity

    for lot_id, demand in lot_demand.items():
        lot = StockLot.objects.get(pk=lot_id)
        other = _active_reserved_for_lot(lot)  # эта бронь ещё draft → не входит
        if other + demand > lot.quantity:
            raise ReservationError(
                f"Лот #{lot_id}: доступно для резерва {lot.quantity - other}, нужно {demand}."
            )

    reservation.status = Reservation.Status.ACTIVE
    reservation.save(update_fields=["status", "updated_at"])
    _recompute_for_lines(lines)
    return reservation


@transaction.atomic
def cancel_reservation(reservation, *, by=None, reason="") -> Reservation:
    """Отменить бронь: освобождает остаток (если была активной)."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status in (Reservation.Status.CANCELED, Reservation.Status.EXPIRED):
        return reservation
    if reservation.status == Reservation.Status.CONVERTED:
        raise ReservationError("Резерв уже продан — отмена недоступна.")
    was_active = reservation.status == Reservation.Status.ACTIVE
    lines = list(reservation.lines.select_related("part_item", "stock_lot"))
    reservation.status = Reservation.Status.CANCELED
    reservation.canceled_at = timezone.now()
    reservation.save(update_fields=["status", "canceled_at", "updated_at"])
    if was_active:
        _recompute_for_lines(lines)
    return reservation


@transaction.atomic
def expire_reservations(*, now=None, by=None) -> int:
    """Перевести просроченные активные брони в expired и обновить кэш.

    `reserved_for` исключает просроченные сразу (по `expires_at`); команда лишь
    нормализует статус и пересобирает строки кэша. Возвращает число резервов.
    """
    now = now or timezone.now()
    expired = list(
        Reservation.objects.select_for_update().filter(
            status=Reservation.Status.ACTIVE,
            expires_at__isnull=False,
            expires_at__lte=now,
        )
    )
    for reservation in expired:
        lines = list(reservation.lines.select_related("part_item", "stock_lot"))
        reservation.status = Reservation.Status.EXPIRED
        reservation.canceled_at = now
        reservation.save(update_fields=["status", "canceled_at", "updated_at"])
        _recompute_for_lines(lines)
    return len(expired)
