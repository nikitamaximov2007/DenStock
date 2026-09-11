"""Anonymous public cart kept only in the customer's signed session cookie.

The cart is a list of wishes, not a document. It never reserves, sells or
writes to the database: the public runtime's session engine is
``signed_cookies``, so the cart lives entirely in the customer's cookie. It
holds nothing but ``{public_id: quantity}``; price, availability, article and name
are re-read from the canonical facades every time the cart is shown, so a
client-supplied price or a stale quantity can never become authoritative.

A line for a part with no available stock is a supply inquiry ("Узнать о
поставке"). That matches the request domain, which accepts an inquiry only
for a part that has nothing available at submission time. Sending the cart
as a customer request is ``public_requests``; the cart itself never writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from .public_catalog import PartCard, cards_by_id, public_parts

CART_SESSION_KEY = "public_catalog_cart"
MAX_CART_LINES = 50
MAX_CART_QUANTITY = 1000
ZERO = Decimal("0")

LINE_OK = "ok"
LINE_INQUIRY = "inquiry"
LINE_SHORT = "short"


class CartError(ValueError):
    """A customer-facing reason why the cart did not change."""


def _clean_key(key) -> str | None:
    try:
        return str(UUID(str(key)))
    except (TypeError, ValueError, AttributeError):
        return None


def _clean_quantity(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_CART_QUANTITY else None


def read_cart(session) -> dict[str, int]:
    """The stored cart, with anything malformed dropped instead of trusted."""
    raw = session.get(CART_SESSION_KEY, {})
    if not isinstance(raw, dict):
        return {}
    cart: dict[str, int] = {}
    for key, value in raw.items():
        clean_key, quantity = _clean_key(key), _clean_quantity(value)
        if clean_key and quantity and len(cart) < MAX_CART_LINES:
            cart[clean_key] = quantity
    return cart


def _write(session, cart: dict[str, int]) -> None:
    session[CART_SESSION_KEY] = cart


def cart_size(session) -> int:
    return len(read_cart(session))


def quantity_in_cart(session, public_id) -> int:
    return read_cart(session).get(str(public_id), 0)


def parse_quantity(raw) -> int:
    text = str(raw if raw is not None else "").strip()
    # ASCII only: int() would also accept other scripts' digits.
    if not (text.isascii() and text.isdigit()) or len(text) > 6:
        raise CartError(f"Укажите количество от 1 до {MAX_CART_QUANTITY}.")
    quantity = int(text)
    if not 1 <= quantity <= MAX_CART_QUANTITY:
        raise CartError(f"Укажите количество от 1 до {MAX_CART_QUANTITY}.")
    return quantity


def set_line(session, card: PartCard, raw_quantity) -> int:
    """Put a part in the cart with this quantity, re-checking availability now."""
    quantity = parse_quantity(raw_quantity)
    available = card.facts.available_quantity
    if ZERO < available < quantity:
        raise CartError(
            f"Сейчас доступно {_quantity_text(available)} {card.facts.unit.short_name}."
            " Уменьшите количество."
        )
    cart = read_cart(session)
    key = str(card.facts.public_id)
    if key not in cart and len(cart) >= MAX_CART_LINES:
        raise CartError(f"В корзине может быть не больше {MAX_CART_LINES} позиций.")
    cart[key] = quantity
    _write(session, cart)
    return quantity


def remove_line(session, public_id) -> None:
    cart = read_cart(session)
    if cart.pop(str(public_id), None) is not None:
        _write(session, cart)


def _quantity_text(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.quantize(Decimal("1")))
    return format(value.normalize(), "f").replace(".", ",")


@dataclass(frozen=True, slots=True)
class CartLine:
    card: PartCard
    quantity: int
    state: str
    line_total: Decimal | None
    # Warehouse key for the request hand-off; server-side only, never rendered.
    part_id: int = field(default=0, repr=False)


@dataclass(frozen=True, slots=True)
class CartView:
    lines: list[CartLine]
    total: Decimal
    priced_lines: int
    unpriced_lines: int
    inquiry_lines: int
    short_lines: int
    removed_lines: int

    @property
    def is_empty(self) -> bool:
        return not self.lines


def _line_state(available: Decimal, quantity: int) -> str:
    if available <= ZERO:
        return LINE_INQUIRY
    if available < quantity:
        return LINE_SHORT
    return LINE_OK


def build_cart_view(session) -> CartView:
    """Hydrate the cart from current data and prune lines that left the catalog."""
    stored = read_cart(session)
    ids_by_key = {
        str(public_id): part_id
        for part_id, public_id in public_parts()
        .filter(public_id__in=list(stored))
        .values_list("pk", "public_id")
    }
    removed = [key for key in stored if key not in ids_by_key]
    if removed or session.get(CART_SESSION_KEY, {}) != stored:
        # Only the customer's own cookie changes: lines of parts that left the
        # catalog and malformed leftovers are dropped from it.
        _write(session, {key: qty for key, qty in stored.items() if key in ids_by_key})
    cards = cards_by_id(ids_by_key.values())

    lines: list[CartLine] = []
    total = ZERO
    counts = {"priced": 0, "unpriced": 0, "inquiry": 0, "short": 0}
    for key, quantity in stored.items():
        card = cards.get(ids_by_key.get(key))
        if card is None:
            continue
        state = _line_state(card.facts.available_quantity, quantity)
        price = card.facts.price.price_rub
        line_total = price * quantity if price is not None else None
        if state == LINE_INQUIRY:
            counts["inquiry"] += 1
        elif state == LINE_SHORT:
            counts["short"] += 1
        elif line_total is None:
            counts["unpriced"] += 1
        else:
            counts["priced"] += 1
            total += line_total
        lines.append(
            CartLine(
                card=card,
                quantity=quantity,
                state=state,
                line_total=line_total,
                part_id=ids_by_key[key],
            )
        )
    return CartView(
        lines=lines,
        total=total,
        priced_lines=counts["priced"],
        unpriced_lines=counts["unpriced"],
        inquiry_lines=counts["inquiry"],
        short_lines=counts["short"],
        removed_lines=len(removed),
    )
