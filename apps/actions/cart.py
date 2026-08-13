"""Корзина сканера: один документ на много отсканированных позиций.

Скан НЕ трогает склад. Позиции копятся в ЧЕРНОВИКЕ существующего документа
(`Sale` / `RepairOrder`) — тех же самых, что сканер создавал и раньше, только
теперь документ проводится один раз в конце, а не на каждый скан. Физический
расход по-прежнему делают ТОЛЬКО сервисы `apps.sales` / `apps.repairs` при
проведении (`complete_sale` / `complete_repair_order`), которые заново блокируют
лоты и перепроверяют доступность. Здесь склад не меняется никогда.

Строки в БД остаются полотовыми (FIFO по ячейке): на них держится
себестоимость, партии и возвраты — эту семантику менять нельзя. Пользователь
при этом видит ОДНУ строку на деталь в ячейке: повторный скан той же canonical
`PartType` увеличивает количество существующей позиции, а не добавляет новую.
Личность позиции — canonical деталь, а не строка, пришедшая со сканера.

Два параллельных черновика могут держать один и тот же остаток: это
существующее поведение черновиков (черновик ничего не резервирует). Кто
проведёт первым — тот и получит деталь, второму `complete_*` откажет с
понятной ошибкой. Оверселла не возникает.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from apps.inventory.models import StockLot
from apps.procurement.models import money
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    RepairError,
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.sales.models import Sale
from apps.sales.services import (
    SaleError,
    add_stock_lot_to_sale,
    complete_sale,
    create_sale,
)

from .models import WarehouseAction
from .services import (
    ActionError,
    _manufacturer_snapshot,
    _price_source_number,
    _request_token,
    _split_quantity_over_lots,
    identity_number,
    parse_quantity,
)

KIND_SALE = "sale"
KIND_REPAIR = "repair"
CART_KINDS = (KIND_SALE, KIND_REPAIR)

# Клиента вводят в конце, а документ нужен уже на первый скан: до проведения
# черновик живёт под служебным именем и в отчёты не попадает (отчёты считают
# только проведённые документы).
CART_PLACEHOLDER_NAME = "Черновик сканера"
CART_COMMENT = "Сканер действий"

EMPTY_CART_MESSAGE = "Корзина пуста: отсканируйте хотя бы одну деталь."


@dataclass(frozen=True)
class CartRow:
    """Одна видимая позиция корзины: деталь + ячейка + суммарное количество."""

    part: object
    location: object
    quantity: Decimal
    unit_price: Decimal | None
    total_price: Decimal | None

    @property
    def key(self) -> str:
        """Стабильный ключ строки для форм (деталь + ячейка)."""
        return f"{self.part.pk}:{self.location.pk}"


def parse_row_key(value: str) -> tuple[int, int]:
    """Разобрать ключ строки корзины в (part_id, location_id)."""
    part_id, _, location_id = (value or "").partition(":")
    try:
        return int(part_id), int(location_id)
    except (TypeError, ValueError) as exc:
        raise ActionError("Некорректная позиция корзины.") from exc


def cart_kind(cart) -> str:
    return KIND_SALE if isinstance(cart, Sale) else KIND_REPAIR


def _document_model(kind: str):
    if kind == KIND_SALE:
        return Sale
    if kind == KIND_REPAIR:
        return RepairOrder
    raise ActionError("Неизвестный тип корзины.")


def _draft_status(cart):
    return Sale.Status.DRAFT if isinstance(cart, Sale) else RepairOrder.Status.DRAFT


def _ensure_draft(cart) -> None:
    if cart.status != _draft_status(cart):
        raise ActionError("Документ уже проведён или отменён — корзину менять нельзя.")


def _lines(cart):
    return cart.lines.select_related("part_type", "stock_lot", "stock_lot__location")


def open_cart(kind: str, *, by=None):
    """Создать пустой черновик-корзину нужного типа."""
    if kind == KIND_SALE:
        return create_sale(customer_name=CART_PLACEHOLDER_NAME, comment=CART_COMMENT, by=by)
    if kind == KIND_REPAIR:
        return create_repair_order(
            customer_name=CART_PLACEHOLDER_NAME, comment=CART_COMMENT, by=by
        )
    raise ActionError("Неизвестный тип корзины.")


def load_cart(kind: str, cart_id):
    """Найти открытую корзину по id. Проведённая/отменённая — не корзина."""
    model = _document_model(kind)
    if not cart_id:
        return None
    cart = model.objects.filter(pk=cart_id).first()
    if cart is None or cart.status != model.Status.DRAFT:
        return None
    return cart


def cart_rows(cart) -> list[CartRow]:
    """Видимые позиции корзины: по одной на деталь в ячейке, FIFO-лоты свёрнуты."""
    grouped: dict[tuple[int, int], dict] = {}
    for line in _lines(cart).order_by("pk"):
        if line.stock_lot_id is None:
            continue  # поштучные экземпляры сканером не набираются
        key = (line.part_type_id, line.stock_lot.location_id)
        row = grouped.setdefault(
            key,
            {
                "part": line.part_type,
                "location": line.stock_lot.location,
                "quantity": Decimal("0"),
                "unit_price": getattr(line, "unit_price", None),
            },
        )
        row["quantity"] += line.quantity
    rows = []
    for row in grouped.values():
        unit_price = row["unit_price"]
        rows.append(
            CartRow(
                part=row["part"],
                location=row["location"],
                quantity=row["quantity"],
                unit_price=unit_price,
                total_price=money(unit_price * row["quantity"]) if unit_price is not None else None,
            )
        )
    return rows


def cart_total(cart) -> Decimal:
    """Сумма корзины (для продажи). Для ремонта цены клиента нет — ноль."""
    return money(sum((row.total_price or Decimal("0") for row in cart_rows(cart)), Decimal("0")))


def cart_quantity(cart) -> Decimal:
    return sum((row.quantity for row in cart_rows(cart)), Decimal("0"))


def find_row(cart, part, location) -> CartRow | None:
    for row in cart_rows(cart):
        if row.part.pk == part.pk and row.location.pk == location.pk:
            return row
    return None


def _drop_row_lines(cart, part, location) -> None:
    stale = [
        line.pk
        for line in _lines(cart)
        if line.stock_lot_id
        and line.part_type_id == part.pk
        and line.stock_lot.location_id == location.pk
    ]
    if stale:
        cart.lines.filter(pk__in=stale).delete()


@transaction.atomic
def set_row_quantity(cart, part, location, quantity, *, by=None) -> CartRow | None:
    """Задать итоговое количество детали в ячейке (0 — убрать позицию).

    Позиция пересобирается по лотам заново (FIFO): доступность проверяют те же
    сервисы документа, что и раньше. Склад при этом не меняется — в черновике
    есть только строки.
    """
    _ensure_draft(cart)
    quantity = parse_quantity(quantity, allow_zero=True)
    _drop_row_lines(cart, part, location)
    if quantity == 0:
        return None
    lots = list(
        StockLot.objects.select_for_update()
        .filter(part_type=part, location=location, status=StockLot.Status.AVAILABLE)
        .order_by("created_at", "pk")
    )
    portions = _split_quantity_over_lots(lots, quantity)
    unit_price = part.recommended_price or Decimal("0")
    try:
        for lot, portion in portions:
            if isinstance(cart, Sale):
                add_stock_lot_to_sale(cart, lot, portion, unit_price=unit_price, by=by)
            else:
                add_stock_lot_to_repair_order(cart, lot, portion, by=by)
    except (SaleError, RepairError) as exc:
        raise ActionError(str(exc)) from exc
    return find_row(cart, part, location)


@transaction.atomic
def add_scan(cart, part, location, *, quantity=Decimal("1"), by=None) -> CartRow:
    """Добавить скан: та же деталь в той же ячейке — плюс к существующей позиции."""
    _ensure_draft(cart)
    quantity = parse_quantity(quantity)
    current = find_row(cart, part, location)
    already = current.quantity if current else Decimal("0")
    return set_row_quantity(cart, part, location, already + quantity, by=by)


@transaction.atomic
def remove_row(cart, part, location, *, by=None) -> None:
    """Убрать позицию целиком."""
    _ensure_draft(cart)
    _drop_row_lines(cart, part, location)


@transaction.atomic
def clear_cart(cart, *, by=None) -> None:
    """Очистить корзину, сам черновик остаётся открытым."""
    _ensure_draft(cart)
    cart.lines.all().delete()


@transaction.atomic
def discard_cart(cart, *, by=None) -> None:
    """Удалить пустой/ненужный черновик-корзину. Склад не затронут."""
    _ensure_draft(cart)
    cart.lines.all().delete()
    cart.delete()


def _existing_actions_for_token(token):
    if not token:
        return None
    first = WarehouseAction.objects.filter(request_token=token).first()
    if first is None:
        return None
    if first.sale_id:
        return list(WarehouseAction.objects.filter(sale_id=first.sale_id).order_by("pk"))
    if first.repair_order_id:
        return list(
            WarehouseAction.objects.filter(repair_order_id=first.repair_order_id).order_by("pk")
        )
    return [first]


@transaction.atomic
def complete_cart(
    cart, *, customer_comment, by=None, scanned_numbers=None, request_token=None
) -> list[WarehouseAction]:
    """Провести корзину одним документом и записать действия в журнал.

    Списание делает `complete_sale` / `complete_repair_order` — доступность
    перепроверяется там заново под блокировкой лотов. На каждую видимую позицию
    пишется своя запись журнала (отчёт по-прежнему построчный), но все они
    ссылаются на ОДИН документ.
    """
    token = _request_token(request_token)
    replay = _existing_actions_for_token(token)
    if replay:
        return replay

    _ensure_draft(cart)
    customer_comment = (customer_comment or "").strip()
    if not customer_comment:
        raise ActionError("Укажите клиента или комментарий.")
    rows = cart_rows(cart)
    if not rows:
        raise ActionError(EMPTY_CART_MESSAGE)

    scanned_numbers = scanned_numbers or {}
    is_sale = isinstance(cart, Sale)
    # Снимки личности собираем ДО проведения: после списания лоты меняются.
    snapshots = [
        {
            "part": row.part,
            "location": row.location,
            "quantity": row.quantity,
            "unit_price": row.unit_price or Decimal("0"),
            "scanned": scanned_numbers.get(row.key, ""),
        }
        for row in rows
    ]

    cart.customer_name = customer_comment
    cart.comment = CART_COMMENT
    cart.save(update_fields=["customer_name", "comment", "updated_at"])

    try:
        document = complete_sale(cart, by=by) if is_sale else complete_repair_order(cart, by=by)
    except (SaleError, RepairError) as exc:
        raise ActionError(str(exc)) from exc

    action_type = WarehouseAction.Type.SALE if is_sale else WarehouseAction.Type.REPAIR
    actions = []
    for index, snap in enumerate(snapshots):
        part = snap["part"]
        unit_price = snap["unit_price"]
        actions.append(
            WarehouseAction.objects.create(
                action_type=action_type,
                # Токен защищает от двойной отправки формы: он один на весь
                # документ, поэтому висит на первой записи.
                request_token=token if index == 0 else None,
                part_type=part,
                part_number=identity_number(part, snap["scanned"]),
                part_name=part.name,
                manufacturer_name=_manufacturer_snapshot(part),
                location=snap["location"],
                location_code=snap["location"].code,
                quantity=snap["quantity"],
                unit_price_rub=unit_price,
                total_price_rub=money(unit_price * snap["quantity"]),
                price_source_number=_price_source_number(part),
                customer_comment=customer_comment,
                sale=document if is_sale else None,
                repair_order=None if is_sale else document,
                created_by=by,
            )
        )
    return actions
