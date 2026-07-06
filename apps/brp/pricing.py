"""Layer 31 — расчёт цены клиента из розницы BRP. Decimal, БЕЗ округления.

Формула (ничего не округляем: ни до 10, ни до 100, копейки сохраняются):

    цена_клиента_руб = розница_USD * курс * (1 + наценка_% / 100)

Примеры при курсе 105 и наценке 40%:
    100 USD  -> 100 * 105 * 1.40 = 14700 ₽
    99.99 USD -> 99.99 * 105 * 1.40 = 14698.53 ₽

Терминология: 40% — это НАЦЕНКА поверх пересчитанной розницы (не «маржа»).
"""
from decimal import Decimal, InvalidOperation

from .models import BrpPricingSettings

HUNDRED = Decimal("100")
ONE = Decimal("1")


def customer_price_rub(retail_price_usd, usd_rate, markup_percent):
    """Точная цена клиента в рублях. None, если розничной цены нет.

    Только Decimal-математика (float запрещён), результат не квантуется.
    """
    if retail_price_usd in (None, ""):
        return None
    try:
        retail = Decimal(str(retail_price_usd))
        rate = Decimal(str(usd_rate))
        markup = Decimal(str(markup_percent))
    except InvalidOperation:
        return None
    return retail * rate * (ONE + markup / HUNDRED)


def current_customer_price_rub(retail_price_usd):
    """Цена клиента по ТЕКУЩИМ настройкам (для превью каталога)."""
    settings = BrpPricingSettings.get()
    return customer_price_rub(
        retail_price_usd, settings.brp_usd_rate, settings.brp_markup_percent
    )
