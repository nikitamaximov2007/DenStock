from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def whole_number(value):
    """Display Decimal values as whole units using ROUND_HALF_UP."""
    if value in (None, ""):
        return ""
    try:
        rounded = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return value
    return format(rounded, "f")
