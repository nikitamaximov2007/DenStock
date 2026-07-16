from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.brp.models import BrpPricingSettings
from apps.brp.pricing import customer_price_rub as brp_customer_price_rub
from apps.catalog.forms import PriceSettingsForm
from apps.polaris.models import PolarisPricingSettings
from apps.polaris.pricing import customer_price_rub as polaris_customer_price_rub
from apps.warehouse.models import ValuationSettings


@pytest.mark.parametrize(
    ("rate", "brp", "polaris", "expected"),
    [
        ("105", "40", "40", (Decimal("105"), Decimal("40"), Decimal("40"))),
        ("105.5", "40.25", "35.75", (Decimal("105.5"), Decimal("40.25"), Decimal("35.75"))),
        ("105,5", "40,25", "35,75", (Decimal("105.5"), Decimal("40.25"), Decimal("35.75"))),
    ],
)
def test_price_settings_form_keeps_decimal_input(rate, brp, polaris, expected):
    form = PriceSettingsForm(
        {
            "current_usd_rate": rate,
            "brp_markup_percent": brp,
            "polaris_markup_percent": polaris,
        }
    )
    assert form.is_valid(), form.errors
    assert (
        form.cleaned_data["current_usd_rate"],
        form.cleaned_data["brp_markup_percent"],
        form.cleaned_data["polaris_markup_percent"],
    ) == expected


def test_price_settings_form_does_not_trim_significant_integer_zeroes():
    form = PriceSettingsForm(
        initial={
            "current_usd_rate": Decimal("105.0000"),
            "brp_markup_percent": Decimal("40.00"),
            "polaris_markup_percent": Decimal("100.00"),
        }
    )
    assert form["current_usd_rate"].value() == "105"
    assert form["brp_markup_percent"].value() == "40"
    assert form["polaris_markup_percent"].value() == "100"


def test_price_settings_inputs_accept_both_decimal_separators_in_any_browser_locale():
    form = PriceSettingsForm()
    for field_name, step in (
        ("current_usd_rate", "0.0001"),
        ("brp_markup_percent", "0.01"),
        ("polaris_markup_percent", "0.01"),
    ):
        widget = form.fields[field_name].widget
        assert widget.input_type == "text"
        assert widget.attrs["inputmode"] == "decimal"
        assert widget.attrs["step"] == step


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("current_usd_rate", "-1"),
        ("current_usd_rate", "text"),
        ("current_usd_rate", "105.12345"),
        ("current_usd_rate", "1000000"),
        ("brp_markup_percent", "-0.01"),
        ("brp_markup_percent", "40.251"),
        ("polaris_markup_percent", "10000"),
    ],
)
def test_price_settings_form_rejects_invalid_values(field, value):
    data = {
        "current_usd_rate": "105",
        "brp_markup_percent": "40",
        "polaris_markup_percent": "40",
    }
    data[field] = value
    form = PriceSettingsForm(data)
    assert not form.is_valid()
    assert field in form.errors


@pytest.mark.django_db
def test_fractional_settings_persist_and_reopen_without_padding(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="price-admin", password="password", is_superuser=True
    )
    client.force_login(user)
    response = client.post(
        reverse("price_settings"),
        {
            "current_usd_rate": "105,5",
            "brp_markup_percent": "40,25",
            "polaris_markup_percent": "35.75",
        },
    )
    assert response.status_code == 302
    assert ValuationSettings.get().current_usd_rate == Decimal("105.5000")
    assert BrpPricingSettings.get().brp_markup_percent == Decimal("40.25")
    assert PolarisPricingSettings.get().polaris_markup_percent == Decimal("35.75")

    html = client.get(reverse("price_settings")).content.decode()
    assert 'id="id_current_usd_rate"' in html and 'value="105.5"' in html
    assert 'id="id_brp_markup_percent"' in html and 'value="40.25"' in html
    assert 'id="id_polaris_markup_percent"' in html and 'value="35.75"' in html


def test_fractional_settings_use_exact_decimal_pricing_for_both_catalogs():
    assert brp_customer_price_rub("35.99", "105.5", "40.25") == Decimal("5325")
    assert polaris_customer_price_rub("100", "105.5", "40.25") == Decimal("14796")


def test_price_preview_uses_integer_decimal_math_only():
    source = (
        Path(settings.BASE_DIR) / "static" / "js" / "price_settings.js"
    ).read_text(encoding="utf-8")
    assert "BigInt" in source
    assert "Number(" not in source
    assert "parseFloat" not in source
    assert "Math.round" not in source
    assert "denominator" in source
