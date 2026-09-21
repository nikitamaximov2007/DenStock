"""«Заказать ещё раз»: a past purchase becomes a NEW current cart, never a copy.

For every historical line the CURRENT system decides: is the part still
public, is it available, what does it cost today. The historical price is
shown for comparison only and is never carried into the cart — the cart holds
nothing but ``{public_id: quantity}`` and re-reads prices itself.

Nothing here creates a request, a sale or a reservation. The preview reads;
«Добавить в корзину» writes only the customer's own cart cookie, and the
customer sends it through the ordinary cart → request form after reviewing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from apps.catalog.public_cart import MAX_CART_QUANTITY, read_cart
from apps.catalog.public_catalog import cards_by_id, public_parts

from .history import Purchase

ZERO = Decimal("0")

OK = "ok"  # public, in stock for the proposed quantity
SHORT = "short"  # public, in stock but less than before
INQUIRY = "inquiry"  # public, none in stock: becomes a supply request
NOT_PUBLIC = "not_public"  # exists, but is not in the public catalog now
MISSING = "missing"  # no such part any more

LINE_NOTES = {
    OK: "",
    SHORT: "Сейчас в наличии меньше, чем в прошлый раз.",
    INQUIRY: "Сейчас нет в наличии - отправим запрос о поставке.",
    NOT_PUBLIC: "Эта позиция сейчас недоступна.",
    MISSING: "Эта позиция сейчас недоступна.",
}


@dataclass(frozen=True)
class ReorderLine:
    name: str
    article: str
    state: str
    proposed_quantity: int
    historical_unit_price: Decimal
    current_price: Decimal | None  # None: «Цена уточняется», never 0 ₽
    public_id: str = ""
    available: Decimal = ZERO

    @property
    def usable(self) -> bool:
        return self.state in {OK, SHORT, INQUIRY}

    @property
    def note(self) -> str:
        return LINE_NOTES[self.state]

    @property
    def price_changed(self) -> bool:
        return self.current_price is not None and self.current_price != self.historical_unit_price

    @property
    def cart_quantity(self) -> int:
        """What goes into the cart: never more than there is when stock is short."""
        if self.state == SHORT:
            return max(1, min(self.proposed_quantity, int(self.available)))
        return self.proposed_quantity


def _proposed(quantity: Decimal) -> int:
    """The historical quantity as a whole cart quantity the customer can edit."""
    return max(1, min(MAX_CART_QUANTITY, math.ceil(quantity)))


def preview(purchase: Purchase) -> list[ReorderLine]:
    part_ids = [line.part_type_id for line in purchase.lines]
    public_ids = set(public_parts().filter(pk__in=part_ids).values_list("pk", flat=True))
    cards = cards_by_id([pid for pid in part_ids if pid in public_ids])
    from apps.catalog.models import PartType

    existing = set(PartType.objects.filter(pk__in=part_ids).values_list("pk", flat=True))
    result = []
    for line in purchase.lines:
        proposed = _proposed(line.quantity)
        card = cards.get(line.part_type_id)
        if card is None:
            state = NOT_PUBLIC if line.part_type_id in existing else MISSING
            result.append(
                ReorderLine(
                    name=line.name,
                    article=line.article,
                    state=state,
                    proposed_quantity=proposed,
                    historical_unit_price=line.unit_price,
                    current_price=None,
                )
            )
            continue
        facts = card.facts
        available = facts.available_quantity
        if available <= ZERO:
            state = INQUIRY
        elif available < proposed:
            state = SHORT
        else:
            state = OK
        price = facts.price.price_rub if facts.price.status == "known" else None
        result.append(
            ReorderLine(
                name=card.display_name,
                article=facts.article,
                state=state,
                proposed_quantity=proposed,
                historical_unit_price=line.unit_price,
                current_price=price,
                public_id=str(facts.public_id),
                available=available,
            )
        )
    return result


def add_to_cart(session, lines: list[ReorderLine]) -> int:
    """Put the usable lines into the customer's cart; returns how many were added.

    A part already in the cart keeps the larger of the two quantities, so a
    reorder never silently shrinks what the customer had chosen themselves.
    """
    from apps.catalog.public_cart import MAX_CART_LINES, _write

    cart = read_cart(session)
    added = 0
    for line in lines:
        if not line.usable or not line.public_id:
            continue
        if line.public_id not in cart and len(cart) >= MAX_CART_LINES:
            break
        cart[line.public_id] = max(cart.get(line.public_id, 0), line.cart_quantity)
        added += 1
    _write(session, cart)
    return added
