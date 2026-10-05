"""Single source of truth for what a PartType's quantity NUMBER means.

Two quantity domains (``quantity_domain``): PIECE, a whole count, and
MEASURED, a physical measure at 0.001 precision. Oil is MEASURED in liters.

шт. (pieces) for a normal part, л (liters) for oil - this module is the only
place that decision is made. Every template/service that needs a unit label
or needs to parse/format an operator-entered quantity should go through here
instead of re-deriving "is this oil" logic locally.

Oil is always fractional liters at 0.001 L precision, stored in the exact
same ``Decimal(max_digits=12, decimal_places=3)`` columns every other bulk
part already uses (``StockLot.quantity``, ``StockMovement.quantity``,
``SaleLine.quantity``, ``RepairIssueLine.quantity``, ...). No new quantity
type, no float, no milliliter-integer parallel engine.
"""

from decimal import Decimal, InvalidOperation

OIL_UNIT_SHORT = "л"
OIL_UNIT_NAME = "литр"
OIL_STEP = Decimal("0.001")


def is_oil_quantity(part_type) -> bool:
    """True when this PartType's quantity number means liters, not pieces."""
    return bool(part_type is not None and part_type.is_oil)


def quantity_unit_short(part_type) -> str:
    """Short unit label for a PartType's quantity number (шт./л/...)."""
    if part_type is None:
        return ""
    if part_type.is_oil:
        return OIL_UNIT_SHORT
    return part_type.unit.short_name if part_type.unit_id else ""


def format_quantity(value, part_type) -> str:
    """A quantity number as people read it, decided by what it measures.

    Oil (liters) is fractional by nature: up to three decimals, no trailing
    zeros, comma as the separator - 1.500 -> "1,5", 2.750 -> "2,75",
    2.000 -> "2". A piece part is a whole number: 1.000 -> "1", 15.000 -> "15".
    A fractional piece count is not a valid piece quantity; it is shown exactly
    ("1,5") so the anomaly stays visible, never rounded to a wrong whole number.
    """
    quantity = Decimal(str(value))
    if is_oil_quantity(part_type):
        return _decimal_text(quantity)
    if quantity == quantity.to_integral_value():
        return format(quantity.quantize(Decimal("1")), "f")
    return _decimal_text(quantity)


def _decimal_text(quantity: Decimal) -> str:
    return format(quantity.normalize(), "f").replace(".", ",")


def quantity_field_label(part_type) -> str:
    """Form/label text for the quantity input itself."""
    return "Объём, л" if is_oil_quantity(part_type) else "Количество, шт."


def parse_quantity_input(raw) -> Decimal:
    """Parse an operator-entered quantity, accepting Russian comma input.

    Works the same for oil liters and normal pieces - the difference is only
    in what the resulting Decimal *means*, never in how it is parsed. Raises
    ``decimal.InvalidOperation`` on unparsable input; callers translate that
    into their own domain error.
    """
    if raw is None:
        raise InvalidOperation("empty quantity")
    text = str(raw).strip().replace(",", ".")
    if not text:
        raise InvalidOperation("empty quantity")
    return Decimal(text)


PIECE_QUANTITY_ERROR = "Для штучной детали количество должно быть целым."


class QuantityDomain:
    """What a quantity number measures, independent of how it is sold.

    PIECE counts whole things: шт., компл., упак. MEASURED is a physical
    measure with 0.001 precision: liters, kilograms, meters. Oil is MEASURED
    (liters of stock); its packages are a separate commercial concept used
    only for customer requests and prices, not a quantity domain.
    """

    PIECE = "piece"
    MEASURED = "measured"


# The measured units seeded by catalog 0002 (name and short name). Any other
# unit, including one staff create later, is counted in pieces until it is
# classified here: whole numbers are the safe default for stock.
MEASURED_UNITS = frozenset({"литр", "л", "килограмм", "кг", "метр", "м"})


def _unit_key(value) -> str:
    return str(value or "").strip().lower().rstrip(".")


def unit_quantity_domain(unit) -> str:
    """Domain of a unit by its name or short name."""
    if unit is None:
        return QuantityDomain.PIECE
    if {_unit_key(unit.name), _unit_key(unit.short_name)} & MEASURED_UNITS:
        return QuantityDomain.MEASURED
    return QuantityDomain.PIECE


def quantity_domain(part_type) -> str:
    """PIECE or MEASURED for a PartType; oil is always MEASURED (liters)."""
    if part_type is None:
        return QuantityDomain.PIECE
    if part_type.is_oil:
        return QuantityDomain.MEASURED
    return unit_quantity_domain(part_type.unit if part_type.unit_id else None)


def is_whole_quantity(quantity) -> bool:
    value = Decimal(str(quantity))
    return value.is_finite() and value == value.to_integral_value()


def validate_part_quantity(quantity, part_type) -> str | None:
    """The quantity rule every document line and stock change shares.

    A MEASURED part (oil, or a part counted in л/кг/м) keeps its 0.001
    precision. A PIECE part must be a whole number: 1.5 is refused, never
    rounded, floored or ceiled into a different count. Returns the refusal
    text, or None; callers raise their own domain error with it. A whole
    number never needs the domain, so it costs no query.
    """
    if is_whole_quantity(quantity):
        return None
    if Decimal(str(quantity)).is_finite() and (
        quantity_domain(part_type) == QuantityDomain.MEASURED
    ):
        return None
    return PIECE_QUANTITY_ERROR


def piece_quantity_form_error(form) -> str | None:
    """The piece-quantity refusal a lot form found, for the view's message."""
    errors = form.errors.get("quantity") or ()
    return PIECE_QUANTITY_ERROR if PIECE_QUANTITY_ERROR in errors else None


def clean_lot_form_quantity(form) -> None:
    """Mirror validate_part_quantity in a form that picks a lot and a quantity."""
    lot = form.cleaned_data.get("lot")
    quantity = form.cleaned_data.get("quantity")
    if lot is None or quantity is None:
        return
    error = validate_part_quantity(quantity, lot.part_type)
    if error:
        form.add_error("quantity", error)
