"""Presentation filters for the public PRO-STOR pages."""

import hashlib
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()

NBSP = "\u00a0"


def _group(digits: str) -> str:
    return f"{int(digits):,}".replace(",", NBSP)


@register.filter
def rub(value):
    """A customer price as shown on the price tag: ``30 047 ₽`` or ``1 234,50 ₽``.

    The value is the canonical price and is never rounded here: kopecks, if a
    price has them, are shown rather than silently changing the amount.
    """
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if not amount.is_finite():
        return ""
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):.2f}".partition(".")
    text = _group(whole) if fraction == "00" else f"{_group(whole)},{fraction}"
    return f"{sign}{text}{NBSP}₽"


@lru_cache(maxsize=16)
def _asset_version(path: str) -> str:
    located = finders.find(path)
    if not located:
        return ""
    with open(located, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:10]


@register.simple_tag
def public_asset(path):
    """Static URL with a content version, so a changed stylesheet is never stale.

    The public process serves assets straight from the image (no manifest
    storage), so the version comes from the file itself, read once per process.
    """
    version = _asset_version(path)
    url = static(path)
    return f"{url}?v={version}" if version else url
