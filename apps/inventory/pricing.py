"""Canonical current customer-price projections.

Receipt-time customer-price snapshots remain immutable historical evidence on
lots and serialized items. They are deliberately not candidates for today's
customer price: the current ``PartType.recommended_price`` is authoritative.
Landed cost never participates in customer pricing, and an absent current
price remains ``None`` rather than becoming zero.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal


def resolve_effective_inventory_customer_price(inventory, current_price) -> Decimal | None:
    """Return the authoritative current price for a selected source.

    ``inventory`` is retained in the signature for callers that already have a
    selected lot or item. Its historical receipt snapshot is intentionally
    ignored; only the current price supplied by the caller is authoritative.
    """
    del inventory
    return current_price if current_price is not None else None


def resolve_current_customer_price(part) -> Decimal | None:
    """Return the current DenisStock customer price for one PartType.

    ``PartType.recommended_price`` is the current price shown by DenisStock.
    Provenance and certification fields explain how that value was obtained,
    but they are not a second price authority.  A non-positive or unusable
    value is an unknown customer price, never a public ``0 ₽`` price.
    """
    price = getattr(part, "recommended_price", None)
    if isinstance(price, Decimal) and price.is_finite() and price > 0:
        return price
    return None


def effective_part_customer_prices(parts: Iterable) -> dict[int, Decimal | None]:
    """Return each part's current customer price without historical fallback."""
    return {
        part.pk: resolve_current_customer_price(part)
        for part in parts
        if part is not None
    }


def attach_effective_customer_price(parts: Iterable) -> None:
    """Set ``part.effective_customer_price`` on every given part (bulk, no N+1)."""
    parts = list(parts)
    prices = effective_part_customer_prices(parts)
    for part in parts:
        if part is not None:
            part.effective_customer_price = prices[part.pk]


# --- Масло: единая формула цены за литр (Sale/Repair/отчёты) -----------------
#
# For an oil PartType, ``PartType.recommended_price`` is the PACKAGE price
# (the whole canister), exactly like it is the per-piece price for a normal
# part - it is never redefined as a per-liter authority (see task: "Do not
# change public price semantics into an independent per-liter authority").
# Price-per-liter is always *derived*: package price / package volume.


def oil_price_per_liter_rub(
    package_price_rub: Decimal, package_volume_l: Decimal
) -> Decimal | None:
    """Exact (unrounded) price per liter. ``None`` if either input is unusable.

    Never money-rounded here: rounding this intermediate value before
    multiplying by the used volume is exactly the cumulative-drift bug this
    module exists to avoid (see ``oil_line_amount_rub``).
    """
    if package_price_rub is None or package_volume_l is None:
        return None
    if not isinstance(package_price_rub, Decimal) or not isinstance(package_volume_l, Decimal):
        return None
    if package_volume_l <= 0:
        return None
    return package_price_rub / package_volume_l


def oil_line_amount_rub(
    *, package_price_rub: Decimal, package_volume_l: Decimal, used_volume_l: Decimal
) -> Decimal | None:
    """Money amount for ``used_volume_l`` litres of an oil package.

    Computed as ``(package_price / package_volume) * used_volume``, rounded to
    money ONCE at the end. Rounding a displayed "price per liter" first and
    multiplying by volume second would drift: e.g. a 1000 ₽ package holding
    3 L gives an exact 333.333... ₽/L; rounding to 333.33 ₽/L first and then
    selling the whole 3 L back would total 999.99 ₽, one kopeck short of the
    package price it came from.
    """
    from apps.procurement.models import money

    price_per_liter = oil_price_per_liter_rub(package_price_rub, package_volume_l)
    if price_per_liter is None or used_volume_l is None:
        return None
    return money(price_per_liter * used_volume_l)


def resolve_oil_package_price_rub(part) -> Decimal | None:
    """Current package price for an oil PartType - the same authority
    (``PartType.recommended_price``) a normal part's current price uses."""
    return resolve_current_customer_price(part)


@dataclass(frozen=True)
class OilAvailabilityRow:
    """Context an operator needs to sell/issue oil: what's on hand, what it costs."""

    part_type_id: int
    part_type_name: str
    package_volume_l: Decimal
    package_price_rub: Decimal | None
    price_per_liter_rub: Decimal | None
    available_l: Decimal


def oil_availability_rows(part_types: Iterable) -> list[OilAvailabilityRow]:
    """Один расчёт объёма упаковки / цены / цены за литр / наличия для UI.

    Единая точка, которую используют Sale/Repair/поиск/публичный каталог -
    чтобы формула цены за литр не дублировалась в каждом месте отдельно.
    """
    from apps.inventory.availability import available_totals
    from apps.procurement.models import money

    parts = [part for part in part_types if part is not None and part.is_oil]
    if not parts:
        return []
    totals = available_totals(part.pk for part in parts)
    rows = []
    for part in parts:
        package_price = resolve_oil_package_price_rub(part)
        price_per_liter = None
        if package_price is not None and part.oil_package_volume_l:
            exact = oil_price_per_liter_rub(package_price, part.oil_package_volume_l)
            price_per_liter = money(exact) if exact is not None else None
        rows.append(
            OilAvailabilityRow(
                part_type_id=part.pk,
                part_type_name=part.name,
                package_volume_l=part.oil_package_volume_l,
                package_price_rub=package_price,
                price_per_liter_rub=price_per_liter,
                available_l=totals.get(part.pk, Decimal("0")),
            )
        )
    return rows
