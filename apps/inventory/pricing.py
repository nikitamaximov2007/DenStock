"""Canonical current customer-price projections.

Receipt-time customer-price snapshots remain immutable historical evidence on
lots and serialized items. They are deliberately not candidates for today's
customer price: the current ``PartType.recommended_price`` is authoritative.
Landed cost never participates in customer pricing, and an absent current
price remains ``None`` rather than becoming zero.
"""

from collections.abc import Iterable
from decimal import Decimal


def resolve_effective_inventory_customer_price(inventory, current_price) -> Decimal | None:
    """Return the authoritative current price for a selected source.

    ``inventory`` is retained in the signature for callers that already have a
    selected lot or item. Its historical receipt snapshot is intentionally
    ignored; only the current price supplied by the caller is authoritative.
    """
    del inventory
    return current_price if current_price is not None else None


def effective_part_customer_prices(parts: Iterable) -> dict[int, Decimal | None]:
    """Return each part's current customer price without historical fallback."""
    return {
        part.pk: part.recommended_price
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
