from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

from apps.catalog.quantity_units import quantity_unit_short

register = template.Library()


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _group(value: Decimal) -> str:
    return format(value, ",f").replace(",", " ")


@register.filter
def money_int(value):
    """Display final ruble amounts as whole rubles using ROUND_HALF_UP."""
    decimal = _decimal(value)
    if decimal is None:
        return "-"
    if decimal is False:
        return value
    return _group(decimal.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@register.filter
def money_rub(value):
    return f"{money_int(value)} ₽" if value not in (None, "") else "-"


@register.filter
def whole_number(value):
    """Backward-compatible alias for the ruble whole-number formatter."""
    return money_int(value)


@register.filter
def quantity_int(value):
    """Display quantities compactly without changing fractional values."""
    decimal = _decimal(value)
    if decimal is None:
        return "-"
    if decimal is False:
        return value
    if decimal == decimal.to_integral_value():
        return _group(decimal.quantize(Decimal("1")))
    return format(decimal.normalize(), "f").replace(".", ",")


@register.filter
def quantity_number(value):
    """Backward-compatible alias for the physical quantity formatter."""
    return quantity_int(value)


@register.filter
def part_quantity_unit(part_type):
    """шт./л label for a PartType's quantity number - see quantity_units.py."""
    return quantity_unit_short(part_type)


@register.filter
def quantity_with_unit(value, part_type):
    """'7,3 л' for oil, '5 шт.' for a normal part - number + correct unit in one call."""
    unit = quantity_unit_short(part_type)
    number = quantity_int(value)
    return f"{number} {unit}".strip() if unit else number
