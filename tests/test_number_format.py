from decimal import Decimal

from apps.core.templatetags.number_format import money_int, money_rub, quantity_int


def test_money_int_uses_decimal_round_half_up_and_groups_thousands():
    assert money_int(Decimal("1616.00")) == "1 616"
    assert money_int(Decimal("2645.49")) == "2 645"
    assert money_int(Decimal("2645.50")) == "2 646"
    assert money_int(Decimal("-2645.50")) == "-2 646"
    assert money_int(Decimal("0")) == "0"
    assert money_int(None) == "—"
    assert money_rub(Decimal("3600")) == "3 600 ₽"


def test_quantity_int_preserves_fractional_values_without_rounding():
    assert quantity_int(Decimal("7.000")) == "7"
    assert quantity_int(Decimal("1000.000")) == "1 000"
    assert quantity_int(Decimal("1.500")) == "1,5"
    assert quantity_int(Decimal("1.250")) == "1,25"
    assert quantity_int(None) == "—"
