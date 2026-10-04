"""Single source of truth for what a PartType's quantity NUMBER means.

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
