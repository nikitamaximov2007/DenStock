"""«Провести продажу» is a blue primary button in every state.

The bug: `.inline-form button` (specificity 0,1,1) painted every button inside
an inline form white, outranking `.btn--primary` (0,1,0). The primary button
kept its white text, so it showed as an empty white box and turned blue only on
:hover (`.btn--primary:hover`, 0,2,0). `.toolbar button` did the same to the
border. A container rule may size a button, but painting it (background or
border) must leave design-system `.btn` buttons alone.
"""
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.sales.services import create_sale
from tests.test_sales import admin, data, make_user  # noqa: F401

CSS = Path(settings.BASE_DIR) / "static" / "css" / "app.css"
PAINT = re.compile(r"(?:^|;)\s*(background(?:-color)?|border(?:-color)?)\s*:", re.M)


def _rules(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selectors, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        for selector in selectors.split(","):
            yield " ".join(selector.split()), body


def _paints_btn_buttons(selector, body):
    """A descendant `button` selector that paints and does not exclude .btn."""
    *ancestors, last = selector.split(" ")
    return (
        ancestors
        and re.match(r"button(?![-\w])", last)
        and ":not(.btn)" not in last
        and not re.search(r":(hover|focus|active|disabled|focus-visible)", last)
        and bool(PAINT.search(body))
    )


def _violations(css):
    return [sel for sel, body in _rules(css) if _paints_btn_buttons(sel, body)]


def test_no_container_rule_paints_over_a_design_system_button():
    assert _violations(CSS.read_text(encoding="utf-8")) == []


def test_the_check_catches_the_rules_that_caused_the_bug():
    """Negative control: the exact rules from before the fix are flagged."""
    old = """
    .inline-form button { padding: 6px 10px; cursor: pointer; border: 1px solid var(--line);
      border-radius: 6px; background: #fff; }
    .toolbar input, .toolbar select, .toolbar button { padding: 8px 10px; font-size: 15px;
      border: 1px solid var(--line); border-radius: 8px; }
    """
    assert _violations(old) == [".inline-form button", ".toolbar button"]


def test_the_primary_variant_paints_the_button_blue():
    rules = dict(_rules(CSS.read_text(encoding="utf-8")))
    assert "background: var(--accent)" in rules[".btn--primary"]
    assert "color: #fff" in rules[".btn--primary"]


@pytest.mark.django_db
def test_complete_sale_button_is_a_primary_button_in_an_inline_form(client, data):  # noqa: F811
    sale = create_sale(customer_name="Иван", by=data["admin"])
    client.force_login(data["admin"])
    html = client.get(reverse("sale_detail", args=[sale.pk])).content.decode()

    action = reverse("sale_complete", args=[sale.pk])
    form = re.search(rf'<form[^>]*action="{action}"[^>]*>(.*?)</form>', html, re.S)
    assert form, "the draft sale page has no completion form"
    button = re.search(r'<button[^>]*class="([^"]*)"[^>]*>\s*Провести продажу', form.group(1))
    assert button, "«Провести продажу» button is missing"
    assert {"btn", "btn--primary"} <= set(button.group(1).split())
    assert "style=" not in form.group(1)  # no inline override that would hide the variant
