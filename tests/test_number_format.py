from decimal import Decimal

from apps.core.templatetags.number_format import quantity_number, whole_number


def test_whole_number_uses_decimal_round_half_up():
    assert whole_number(Decimal("1.000")) == "1"
    assert whole_number(Decimal("2645.00")) == "2645"
    assert whole_number(Decimal("2.5")) == "3"
    assert whole_number(Decimal("2.49")) == "2"


def test_quantity_number_preserves_fractional_values():
    assert quantity_number(Decimal("1.000")) == "1"
    assert quantity_number(Decimal("1.500")) == "1.5"
    assert quantity_number(Decimal("1.250")) == "1.25"
