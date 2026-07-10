"""Customer price calculation for Polaris catalog rows."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from apps.warehouse.models import ValuationSettings

from .models import PolarisPricingSettings

HUNDRED = Decimal("100")
ONE = Decimal("1")
WHOLE_RUB = Decimal("1")


def customer_price_rub(retail_price_usd, usd_rate, markup_percent):
    """Return whole-ruble customer price or None when retail price is absent."""
    if retail_price_usd in (None, ""):
        return None
    try:
        retail = Decimal(str(retail_price_usd))
        rate = Decimal(str(usd_rate))
        markup = Decimal(str(markup_percent))
    except InvalidOperation:
        return None
    raw = retail * rate * (ONE + markup / HUNDRED)
    return raw.quantize(WHOLE_RUB, rounding=ROUND_HALF_UP)


def current_customer_price_rub(retail_price_usd):
    valuation = ValuationSettings.get()
    settings = PolarisPricingSettings.get()
    return customer_price_rub(
        retail_price_usd, valuation.current_usd_rate, settings.polaris_markup_percent
    )

