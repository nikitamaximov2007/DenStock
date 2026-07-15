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
from apps.inventory.services import recompute_balance_row, sell_part_item, sell_stock_lot
from apps.procurement.models import BatchLine, money
from apps.warehouse.models import StorageLocation

from .models import Reservation, ReservationLine, Sale, SaleLine


class ReservationError(Exception):
    """Невозможно выполнить операцию с резервом."""


class SaleError(Exception):
    """Невозможно выполнить операцию с продажей."""


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


def _active_reserved_for_lot(lot, *, exclude=None) -> Decimal:
    """Сколько количества лота держат активные брони (опц. исключая один резерв)."""
    qs = ReservationLine.objects.filter(_active_q(), stock_lot=lot)
    if exclude is not None:
        qs = qs.exclude(reservation=exclude)
    return qs.aggregate(s=Sum("quantity"))["s"] or Decimal("0")


def _item_actively_reserved(item, *, exclude=None) -> bool:
    """Есть ли активная бронь на этот экземпляр (опц. исключая один резерв)."""
    qs = ReservationLine.objects.filter(_active_q(), part_item=item)
    if exclude is not None:
        qs = qs.exclude(reservation=exclude)
    return qs.exists()


# --- Public API резерва для других потоков (Слой 17: выдача в ремонт) ---------
#
# apps.repairs не должен знать внутреннюю модель брони — он лишь спрашивает «эта
# деталь/лот зарезервированы активной бронью?». Эти обёртки дают стабильную точку
# доступа; зависимость repairs → sales ациклична (sales про repairs не знает).


def is_part_item_reserved(item) -> bool:
    """Зарезервирован ли экземпляр активной бронью (для выдачи в ремонт)."""
    return _item_actively_reserved(item)


def active_reserved_for_lot(lot) -> Decimal:
    """Сколько количества лота держат активные брони (для выдачи в ремонт)."""
    return _active_reserved_for_lot(lot)


def active_reserved_for_lots(lot_ids) -> dict[int, Decimal]:
    """Active reserved quantities for many lots in one query."""
    ids = [getattr(value, "pk", value) for value in lot_ids]
    if not ids:
        return {}
    rows = (
        ReservationLine.objects.filter(_active_q(), stock_lot_id__in=ids)
        .values("stock_lot_id")
        .annotate(quantity=Sum("quantity"))
    )
    return {row["stock_lot_id"]: row["quantity"] or Decimal("0") for row in rows}


def active_reserved_item_ids(item_ids) -> set[int]:
    """IDs of serial items held by an active reservation, in one query."""
    ids = [getattr(value, "pk", value) for value in item_ids]
    if not ids:
        return set()
    return set(
        ReservationLine.objects.filter(_active_q(), part_item_id__in=ids)
        .values_list("part_item_id", flat=True)
        .distinct()
    )


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


# --- Слой 16: продажи (коммерческий документ) --------------------------------
#
# apps/sales ведёт документ: цены, выручка/себестоимость/прибыль, проверка
# резервов, оркестрация. Физическое списание (статус/количество, StockMovement,
# StockBalance) делают inventory.sell_part_item/sell_stock_lot — sales их только
# вызывает и НЕ пишет ledger напрямую.


def create_sale(
    *, customer_name, customer_phone="", comment="", by=None, reservation=None
) -> Sale:
    """Создать черновик продажи."""
    customer_name = (customer_name or "").strip()
    if not customer_name:
        raise SaleError("Не указан клиент.")
    return Sale.objects.create(
        customer_name=customer_name,
        customer_phone=(customer_phone or "").strip(),
        comment=(comment or "").strip(),
        reservation=reservation,
        sold_by=by,
        status=Sale.Status.DRAFT,
    )


def _ensure_sale_draft(sale: Sale) -> None:
    if sale.status != Sale.Status.DRAFT:
        raise SaleError("Продажа уже проведена — изменять нельзя.")


def _freeze_line_costs(line: SaleLine) -> None:
    """Заморозить цену/себестоимость/прибыль строки на момент продажи."""
    if line.part_item_id:
        unit_cost = line.part_item.landed_cost_rub
    else:
        unit_cost = line.stock_lot.landed_unit_cost_rub
    line.total_price = money(line.unit_price * line.quantity)
    line.unit_cost_rub = unit_cost
    line.total_cost_rub = money(unit_cost * line.quantity)
    line.profit_rub = money(line.total_price - line.total_cost_rub)


@transaction.atomic
def add_part_item_to_sale(sale, item, *, unit_price, by=None) -> SaleLine:
    """Добавить экземпляр в продажу (целиком). Доступность с учётом чужих броней."""
    sale = Sale.objects.select_for_update().get(pk=sale.pk)
    _ensure_sale_draft(sale)
    item = PartItem.objects.select_for_update().get(pk=item.pk)
    if item.status != PartItem.Status.AVAILABLE:
        raise SaleError("Продать можно только доступный экземпляр.")
    if SaleLine.objects.filter(sale=sale, part_item=item).exists():
        raise SaleError("Этот экземпляр уже в продаже.")
    if _item_actively_reserved(item, exclude=sale.reservation):
        raise SaleError("Экземпляр зарезервирован другой бронью.")
    unit_price = Decimal(unit_price)
    return SaleLine.objects.create(
        sale=sale, part_type=item.part_type, part_item=item,
        batch=item.batch, batch_line=item.batch_line,
        quantity=Decimal("1"), unit_price=unit_price,
        total_price=money(unit_price * Decimal("1")),
    )


@transaction.atomic
def add_stock_lot_to_sale(sale, lot, quantity, *, unit_price, by=None) -> SaleLine:
    """Добавить количество из лота в продажу. Доступно = qty − чужой резерв − уже в продаже."""
    sale = Sale.objects.select_for_update().get(pk=sale.pk)
    _ensure_sale_draft(sale)
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise SaleError("Количество должно быть больше нуля.")
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.status != StockLot.Status.AVAILABLE:
        raise SaleError("Продать можно только доступный лот.")
    reserved_others = _active_reserved_for_lot(lot, exclude=sale.reservation)
    already_in_sale = (
        SaleLine.objects.filter(sale=sale, stock_lot=lot)
        .aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )
    available = lot.quantity - reserved_others - already_in_sale
    if quantity > available:
        raise SaleError(
            f"Недостаточно: доступно для продажи {available}, запрошено {quantity}."
        )
    unit_price = Decimal(unit_price)
    return SaleLine.objects.create(
        sale=sale, part_type=lot.part_type, stock_lot=lot,
        batch=lot.batch, batch_line=lot.batch_line,
        quantity=quantity, unit_price=unit_price,
        total_price=money(unit_price * quantity),
    )


@transaction.atomic
def remove_sale_line(line, *, by=None) -> None:
    """Снять позицию из черновика продажи."""
    line = SaleLine.objects.select_for_update().select_related("sale").get(pk=line.pk)
    _ensure_sale_draft(line.sale)
    line.delete()


@transaction.atomic
def create_sale_from_reservation(reservation, *, by=None) -> Sale:
    """Собрать черновик продажи из активного резерва (цены — из recommended_price)."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status != Reservation.Status.ACTIVE:
        raise SaleError("Продать можно только из активного резерва.")
    rlines = list(reservation.lines.select_related("part_type", "part_item", "stock_lot"))
    if not rlines:
        raise SaleError("В резерве нет позиций.")
    sale = create_sale(
        customer_name=reservation.customer_name,
        customer_phone=reservation.customer_phone,
        comment=reservation.comment,
        by=by, reservation=reservation,
    )
    for rline in rlines:
        unit_price = rline.part_type.recommended_price or Decimal("0")
        if rline.part_item_id:
            SaleLine.objects.create(
                sale=sale, part_type=rline.part_type, part_item=rline.part_item,
                batch=rline.part_item.batch, batch_line=rline.part_item.batch_line,
                quantity=Decimal("1"), unit_price=unit_price,
                total_price=money(unit_price * Decimal("1")),
            )
        else:
            SaleLine.objects.create(
                sale=sale, part_type=rline.part_type, stock_lot=rline.stock_lot,
                batch=rline.stock_lot.batch, batch_line=rline.stock_lot.batch_line,
                quantity=rline.quantity, unit_price=unit_price,
                total_price=money(unit_price * rline.quantity),
            )
    return sale


def calculate_sale_totals(sale: Sale) -> dict:
    """Чистый расчёт totals из (замороженных) строк продажи."""
    agg = sale.lines.aggregate(
        revenue=Sum("total_price"), cost=Sum("total_cost_rub"), profit=Sum("profit_rub")
    )
    return {
        "revenue": agg["revenue"] or Decimal("0"),
        "cost": agg["cost"] or Decimal("0"),
        "profit": agg["profit"] or Decimal("0"),
    }


@transaction.atomic
def rebuild_sale_totals(sale) -> Sale:
    """Пересчитать totals из ЗАМОРОЖЕННЫХ строк (не из текущего landed cost)."""
    sale = Sale.objects.select_for_update().get(pk=sale.pk)
    t = calculate_sale_totals(sale)
    sale.revenue_total = money(t["revenue"])
    sale.cost_total = money(t["cost"])
    sale.profit_total = money(t["profit"])
    sale.save(update_fields=["revenue_total", "cost_total", "profit_total", "updated_at"])
    return sale


@transaction.atomic
def complete_sale(sale, *, by=None) -> Sale:
    """Провести продажу: списать остаток через inventory.sell_*, заморозить totals.

    Резерв-источник конвертируется ДО списания, чтобы провайдер reserved сразу
    перестал держать доступность (иначе возможен временный отрицательный available).
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)
    if sale.status != Sale.Status.DRAFT:
        raise SaleError("Продажа уже проведена.")
    lines = list(sale.lines.select_related("part_item", "stock_lot", "part_type"))
    if not lines:
        raise SaleError("Нельзя завершить пустую продажу.")

    own_reservation = None
    if sale.reservation_id:
        own_reservation = Reservation.objects.select_for_update().get(pk=sale.reservation_id)
        if own_reservation.status != Reservation.Status.ACTIVE:
            raise SaleError("Связанный резерв не активен.")
        own_reservation.status = Reservation.Status.CONVERTED
        own_reservation.save(update_fields=["status", "updated_at"])

    for line in lines:
        if line.part_item_id:
            item = PartItem.objects.select_for_update().get(pk=line.part_item_id)
            if item.status != PartItem.Status.AVAILABLE:
                raise SaleError(f"Экземпляр {item.internal_number} недоступен.")
            if _item_actively_reserved(item, exclude=own_reservation):
                raise SaleError(
                    f"Экземпляр {item.internal_number} зарезервирован другой бронью."
                )
            line.part_item = item
            _freeze_line_costs(line)
            line.save(update_fields=[
                "total_price", "unit_cost_rub", "total_cost_rub", "profit_rub"
            ])
            sell_part_item(item, by=by, document_id=sale.pk, comment=f"Продажа {sale.number}")
        else:
            lot = StockLot.objects.select_for_update().get(pk=line.stock_lot_id)
            if lot.status != StockLot.Status.AVAILABLE:
                raise SaleError(f"Лот #{lot.pk} недоступен.")
            reserved_others = _active_reserved_for_lot(lot, exclude=own_reservation)
            if line.quantity > lot.quantity - reserved_others:
                raise SaleError(f"Лот #{lot.pk}: недостаточно для продажи.")
            line.stock_lot = lot
            _freeze_line_costs(line)
            line.save(update_fields=[
                "total_price", "unit_cost_rub", "total_cost_rub", "profit_rub"
            ])
            sell_stock_lot(
                lot, line.quantity, by=by, document_id=sale.pk, comment=f"Продажа {sale.number}"
            )

    if own_reservation is not None:
        # Освободить reserved для позиций резерва, не попавших в продажу (если такие есть).
        _recompute_for_lines(
            list(own_reservation.lines.select_related("part_item", "stock_lot"))
        )

    totals = calculate_sale_totals(sale)
    sale.revenue_total = money(totals["revenue"])
    sale.cost_total = money(totals["cost"])
    sale.profit_total = money(totals["profit"])
    sale.status = Sale.Status.COMPLETED
    sale.sold_at = timezone.now()
    sale.sold_by = by or sale.sold_by
    sale.save(update_fields=[
        "revenue_total", "cost_total", "profit_total", "status", "sold_at",
        "sold_by", "updated_at",
    ])
    return sale
