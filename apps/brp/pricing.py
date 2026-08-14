"""Layer 31/32.1 — расчёт цены клиента из оптовой цены BRP. Decimal, целые рубли.

Формула (весь расчёт на Decimal, float запрещён):

    сырая_цена_руб = оптовая_USD * курс * (1 + наценка_% / 100)
    цена_клиента_руб = сырая_цена_руб, округлённая до ЦЕЛОГО рубля
                       (ROUND_HALF_UP, без копеек)

Исходные цены в долларах, курс и наценка НЕ округляются: округляется только
итоговая цена клиента в рублях. Примеры при курсе 105 и наценке 40%:
    7.39 USD  -> 1086.33  -> 1086 ₽
    9.03 USD  -> 1327.41  -> 1327 ₽
    99.99 USD -> 14698.53 -> 14699 ₽

Терминология: 40% — это НАЦЕНКА поверх пересчитанной оптовой цены (не «маржа»).
Историческая безопасность: уже проведённые документы и старые снимки цен
задним числом не переписываются; правило действует для новых расчётов.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from apps.warehouse.models import ValuationSettings

from .models import BrpPricingSettings

HUNDRED = Decimal("100")
ONE = Decimal("1")
WHOLE_RUB = Decimal("1")


def customer_price_rub(wholesale_price_usd, usd_rate, markup_percent):
    """Цена клиента в целых рублях. None, если оптовой цены нет.

    Только Decimal-математика (float запрещён); до целого рубля квантуется
    ТОЛЬКО итог (ROUND_HALF_UP), исходные значения не трогаются.
    """
    if wholesale_price_usd in (None, ""):
        return None
    try:
        wholesale = Decimal(str(wholesale_price_usd))
        rate = Decimal(str(usd_rate))
        markup = Decimal(str(markup_percent))
    except InvalidOperation:
        return None
    raw = wholesale * rate * (ONE + markup / HUNDRED)
    return raw.quantize(WHOLE_RUB, rounding=ROUND_HALF_UP)


# --- Надбавка винтажного склада ----------------------------------------------
#
# Поставщик BRP помечает статусом VIN позиции с винтажного склада и добавляет к
# ним доставку 25 USD. Эта надбавка НЕ записывается в оптовую цену каталога:
# `wholesale_price_usd` обязана остаться исходной ценой производителя, иначе
# следующий импорт того же прайса выглядел бы как изменение цены, а история
# закупок перестала бы сходиться с прайсом поставщика.
#
# Поэтому надбавка это правило слоя цен, а не поле в базе: её размер задан
# поставщиком и одинаков для всех VIN-позиций, отдельного механизма надбавок
# поставщика в проекте нет, и городить настраиваемую подсистему ради одного
# фиксированного значения было бы лишним. Если BRP когда-нибудь начнёт менять
# размер надбавки, значение переедет в BrpPricingSettings без изменения
# вызывающего кода: он и сейчас работает через единый helper ниже.
VIN_STATUS = "VIN"
VIN_SURCHARGE_USD = Decimal("25")


def status_surcharge_usd(brp_status) -> Decimal:
    """Надбавка поставщика по статусу. Ноль для всех статусов, кроме VIN.

    Сравнение по нормализованному статусу: OBS, USE, LIQ, пустой и любой
    неизвестный статус надбавки НЕ получают.
    """
    if str(brp_status or "").strip().upper() == VIN_STATUS:
        return VIN_SURCHARGE_USD
    return Decimal("0")


def effective_wholesale_usd(catalog_part):
    """Оптовая цена, по которой считается цена клиента.

    Обычная позиция: сырая оптовая цена как есть.
    VIN: сырая оптовая цена плюс доставка с винтажного склада.

    Возвращает None, если оптовой цены нет: надбавка сама по себе ценой не
    является и из ничего цену не создаёт.
    """
    if catalog_part is None:
        return None
    raw = getattr(catalog_part, "wholesale_price_usd", None)
    if raw in (None, ""):
        return None
    try:
        raw = Decimal(str(raw))
    except InvalidOperation:
        return None
    return raw + status_surcharge_usd(getattr(catalog_part, "brp_status", ""))


def catalog_part_price_rub(catalog_part, usd_rate, markup_percent):
    """Цена клиента для позиции каталога с учётом надбавки поставщика."""
    return customer_price_rub(
        effective_wholesale_usd(catalog_part), usd_rate, markup_percent
    )


def current_customer_price_rub(wholesale_price_usd):
    """Цена клиента по ТЕКУЩИМ настройкам (для превью каталога)."""
    valuation = ValuationSettings.get()
    settings = BrpPricingSettings.get()
    return customer_price_rub(
        wholesale_price_usd, valuation.current_usd_rate, settings.brp_markup_percent
    )
