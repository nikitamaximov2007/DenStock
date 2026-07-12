from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

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
        return "—"
    if decimal is False:
        return value
    return _group(decimal.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@register.filter
def money_rub(value):
    return f"{money_int(value)} ₽" if value not in (None, "") else "—"


@register.filter
def whole_number(value):
    """Backward-compatible alias for the ruble whole-number formatter."""
    return money_int(value)


@register.filter
def quantity_int(value):
    """Display quantities compactly without changing fractional values."""
    decimal = _decimal(value)
    if decimal is None:
        return "—"
    if decimal is False:
        return value
    if decimal == decimal.to_integral_value():
        return _group(decimal.quantize(Decimal("1")))
    return format(decimal.normalize(), "f").replace(".", ",")


@register.filter
def quantity_number(value):
    """Backward-compatible alias for the physical quantity formatter."""
    return quantity_int(value)
