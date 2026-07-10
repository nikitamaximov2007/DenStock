"""Layer 31/32.1 — расчёт цены клиента из розницы BRP. Decimal, целые рубли.

Формула (весь расчёт на Decimal, float запрещён):

    сырая_цена_руб = розница_USD * курс * (1 + наценка_% / 100)
    цена_клиента_руб = сырая_цена_руб, округлённая до ЦЕЛОГО рубля
                       (ROUND_HALF_UP, без копеек)

Исходные цены в долларах, курс и наценка НЕ округляются: округляется только
итоговая цена клиента в рублях. Примеры при курсе 105 и наценке 40%:
    7.39 USD  -> 1086.33  -> 1086 ₽
    9.03 USD  -> 1327.41  -> 1327 ₽
    99.99 USD -> 14698.53 -> 14699 ₽

Терминология: 40% — это НАЦЕНКА поверх пересчитанной розницы (не «маржа»).
Историческая безопасность: уже проведённые документы и старые снимки цен
задним числом не переписываются; правило действует для новых расчётов.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from apps.warehouse.models import ValuationSettings

from .models import BrpPricingSettings

HUNDRED = Decimal("100")
ONE = Decimal("1")
WHOLE_RUB = Decimal("1")


def customer_price_rub(retail_price_usd, usd_rate, markup_percent):
    """Цена клиента в целых рублях. None, если розничной цены нет.

    Только Decimal-математика (float запрещён); до целого рубля квантуется
    ТОЛЬКО итог (ROUND_HALF_UP), исходные значения не трогаются.
    """
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
    """Цена клиента по ТЕКУЩИМ настройкам (для превью каталога)."""
    valuation = ValuationSettings.get()
    settings = BrpPricingSettings.get()
    return customer_price_rub(
        retail_price_usd, valuation.current_usd_rate, settings.brp_markup_percent
    )
