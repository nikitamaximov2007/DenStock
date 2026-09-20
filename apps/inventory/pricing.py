"""Цена клиента, привязанная к фактическому складскому источнику.

Снимок появляется только у нового поступления, когда склад реально принимает
деталь.  Он не является себестоимостью и не пытается восстановить цену для
старого остатка: отсутствие снимка остаётся отсутствием исторических данных.

Два уровня одного правила «не опускать цену ниже защищённой старой»:

* источник уже выбран (продажа, ремонт, резерв) —
  ``resolve_effective_inventory_customer_price``: MAX(текущая цена, снимок
  этого лота/экземпляра);
* источник ещё не выбран (поиск, карточка, витрина, заявка) —
  ``effective_part_customer_prices``: MAX(текущая цена, наибольший снимок
  среди того, что ещё лежит на складе).  Проданный, списанный или
  исчерпанный источник цену детали больше не держит.

Себестоимость (``landed_*``) здесь никогда не участвует.
"""

from collections.abc import Iterable
from decimal import Decimal

from django.db.models import OuterRef, Subquery

from apps.catalog.models import PartType

from .models import PartItem, StockLot

ZERO = Decimal("0")

# «Ещё на складе»: всё, что может быть продано по собственному снимку.
# Резервный экземпляр остаётся на складе и продаётся из резерва по тому же
# правилу источника, поэтому он тоже держит цену.
PROTECTING_LOT_STATUSES = (
    StockLot.Status.RECEIVING,
    StockLot.Status.AVAILABLE,
    StockLot.Status.QUARANTINE,
)
PROTECTING_ITEM_STATUSES = (
    PartItem.Status.RECEIVING,
    PartItem.Status.AVAILABLE,
    PartItem.Status.RESERVED,
    PartItem.Status.QUARANTINE,
)


def resolve_effective_inventory_customer_price(inventory, current_price) -> Decimal | None:
    """Return the safe default for one item or lot.

    The current canonical ``PartType.recommended_price`` and the immutable
    receipt snapshot are independent candidates.  A manual document price is
    intentionally handled by its caller and never passes through this helper.
    """
    values = [
        value
        for value in (
            current_price,
            getattr(inventory, "receipt_customer_price_snapshot_rub", None),
        )
        if value is not None
    ]
    return max(values) if values else None


def protected_customer_price_floors(part_ids: Iterable[int]) -> dict[int, Decimal]:
    """Highest positive receipt snapshot still in stock, per part.

    Two aggregate queries for any number of parts.  Parts without a
    protecting snapshot are absent from the result: absence means "no floor",
    never zero.
    """
    ids = list(dict.fromkeys(part_id for part_id in part_ids if part_id is not None))
    if not ids:
        return {}
    # Keep both inventory sources in one SQL statement.  Public catalog pages
    # already have a deliberately tight query budget; doing one aggregate per
    # tracking model would add two queries to every page.  Correlated scalar
    # subqueries let the database select each source's maximum while the outer
    # PartType query combines the two values in Python.
    lot_floor = (
        StockLot.objects.filter(
            part_type_id=OuterRef("pk"),
            status__in=PROTECTING_LOT_STATUSES,
            quantity__gt=0,
        )
        .filter(receipt_customer_price_snapshot_rub__gt=ZERO)
        .order_by("-receipt_customer_price_snapshot_rub")
        .values("receipt_customer_price_snapshot_rub")[:1]
    )
    item_floor = (
        PartItem.objects.filter(
            part_type_id=OuterRef("pk"), status__in=PROTECTING_ITEM_STATUSES
        )
        .filter(receipt_customer_price_snapshot_rub__gt=ZERO)
        .order_by("-receipt_customer_price_snapshot_rub")
        .values("receipt_customer_price_snapshot_rub")[:1]
    )
    rows = PartType.objects.filter(pk__in=ids).annotate(
        lot_floor=Subquery(lot_floor), item_floor=Subquery(item_floor)
    ).values_list("pk", "lot_floor", "item_floor")
    floors: dict[int, Decimal] = {}
    for part_id, lot_value, item_value in rows:
        values = [value for value in (lot_value, item_value) if value is not None]
        if values:
            floors[part_id] = max(values)
    return floors


def resolve_effective_part_customer_price(
    current_price: Decimal | None, protected_floor: Decimal | None
) -> Decimal | None:
    """MAX of the known candidates; ``None`` stays unknown, never ``0``."""
    values = [value for value in (current_price, protected_floor) if value is not None]
    return max(values) if values else None


def effective_part_customer_prices(parts: Iterable) -> dict[int, Decimal | None]:
    """Canonical current customer price for parts whose source is not chosen yet."""
    parts = [part for part in parts if part is not None]
    floors = protected_customer_price_floors(part.pk for part in parts)
    return {
        part.pk: resolve_effective_part_customer_price(
            part.recommended_price, floors.get(part.pk)
        )
        for part in parts
    }


def attach_effective_customer_price(parts: Iterable) -> None:
    """Set ``part.effective_customer_price`` on every given part (bulk, no N+1)."""
    parts = [part for part in parts if part is not None]
    prices = effective_part_customer_prices(parts)
    for part in parts:
        part.effective_customer_price = prices[part.pk]
