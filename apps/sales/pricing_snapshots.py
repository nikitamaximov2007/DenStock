"""Immutable customer-price base snapshots for completed sale lines.

This module deliberately deals with the *dealer* base, not warehouse landed
cost.  The resulting snapshot is the only input used for customer-price profit
reports.  It is captured once while a sale is posted and is never refreshed.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist

from apps.brp.pricing import customer_price_rub, effective_wholesale_usd
from apps.catalog.services import get_current_price_settings
from apps.counting.services import find_brp_price_source
from apps.polaris.services import find_polaris_price_source

LEGACY_RECONSTRUCTION_RATE = Decimal("105")


@dataclass(frozen=True)
class UnmarkedPrice:
    source: str
    dealer_unit_usd: Decimal
    usd_rate: Decimal
    unmarked_unit_price_rub: Decimal


def _positive(value):
    if value is None:
        return None
    value = Decimal(str(value))
    return value if value > 0 else None


def authoritative_dealer_unit_usd(part):
    """Return ``(source, USD, reason)`` from an authoritative linked source.

    The BRP and Polaris lookups intentionally reuse the same source resolution
    as the catalog repricer, including BRP's VIN surcharge.  A package size is
    never involved: all four catalog fields are prices for one unit.
    """
    try:
        link = part.brp_link
    except ObjectDoesNotExist:
        link = None
    if link is not None:
        if not link.brp_part.is_current:
            return None, None, "brp_catalog_not_current"
        source = find_brp_price_source(link.brp_part.material_no_norm, link.brp_part)
        usd = _positive(effective_wholesale_usd(source)) if source else None
        return ("brp", usd, "" if usd else "brp_wholesale_missing")

    try:
        link = part.polaris_link
    except ObjectDoesNotExist:
        link = None
    if link is not None:
        source = find_polaris_price_source(link.polaris_part.part_number_norm, link.polaris_part)
        usd = _positive(source.wholesale_price_usd) if source else None
        return ("polaris", usd, "" if usd else "polaris_wholesale_missing")

    try:
        entry = part.aftermarket_catalog_entry
    except ObjectDoesNotExist:
        entry = None
    if entry is not None:
        usd = _positive(entry.dealer_cost_usd)
        return ("aftermarket", usd, "" if usd else "aftermarket_dealer_cost_missing")

    try:
        entry = part.arctic_cat_catalog_entry
    except ObjectDoesNotExist:
        entry = None
    if entry is not None:
        usd = _positive(entry.dealer_price_usd)
        return ("arctic_cat", usd, "" if usd else "arctic_dealer_price_missing")
    return None, None, "no_authoritative_link"


def resolve_unmarked_price(part, *, usd_rate) -> tuple[UnmarkedPrice | None, str]:
    source, dealer_usd, reason = authoritative_dealer_unit_usd(part)
    if dealer_usd is None:
        return None, reason
    # customer_price_rub with zero markup is the project-wide Decimal and
    # ROUND_HALF_UP whole-ruble convention for a USD-to-RUB unit price.
    unmarked = customer_price_rub(dealer_usd, usd_rate, Decimal("0"))
    return UnmarkedPrice(source, dealer_usd, Decimal(str(usd_rate)), unmarked), ""


def capture_current_unmarked_price(part) -> tuple[UnmarkedPrice | None, str]:
    return resolve_unmarked_price(part, usd_rate=get_current_price_settings().current_usd_rate)
