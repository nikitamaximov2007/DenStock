"""Финансовая оценка склада: три показателя для страницы статистики.

1. Закупочная стоимость склада — во сколько обойдётся повторно заказать весь
   текущий физический остаток по БАЗОВЫМ ценам прайсов производителей и курсу
   из настройки «Курс для оценки закупочной стоимости» (по умолчанию 105 ₽/$).
   BRP: retail_price_usd (база формулы клиентской цены, ДО наценки);
   Polaris: wholesale_price_usd («ОПТОВАЯ»; «РОЗНИЦА» закупкой не считается).
2. Оценка склада по цене продажи — весь физический остаток по действующим
   клиентским ценам (существующие effective-price сервисы BRP/Polaris;
   формулы цен здесь НЕ дублируются).
3. Потенциальная прибыль = оценка продажи − закупочная стоимость.

Это оценка повторного заказа, а НЕ бухгалтерская себестоимость: доставка,
таможня, налоги и комиссии не учитываются (фактическая себестоимость будет
уточняться по реальным поступлениям — отдельная будущая задача).

Правила identity: exact номер детали остаётся личностью; replacement (BRP)
и superseded (Polaris) используются ТОЛЬКО как источник цены при нулевой
цене exact-позиции. Позиции без закупочной цены не считаются «нулём»: они
исключаются из закупочной стоимости и показываются отдельным счётчиком.

Read-only: модуль ничего не пишет (остатки, движения, документы не меняются).
Производительность: агрегаты по остаткам (2 запроса), карточки с prefetch
связей (1 запрос), точечные запросы источника цены только для нулевых цен;
каталоги BRP/Polaris целиком не обходятся.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Count, Q, Sum

from apps.brp.models import BrpPricingSettings
from apps.brp.pricing import customer_price_rub as brp_customer_price_rub
from apps.catalog.models import PartType
from apps.counting.services import find_brp_price_source
from apps.inventory.models import PartItem, StockLot
from apps.inventory.services import ITEM_PHYSICAL_STATUSES, LOT_PHYSICAL_STATUSES
from apps.polaris.models import PolarisCatalogPart, PolarisPricingSettings
from apps.polaris.pricing import customer_price_rub as polaris_customer_price_rub
from apps.polaris.services import find_polaris_price_source
from apps.procurement.models import money
from apps.warehouse.models import ValuationSettings

DEC0 = Decimal("0")


@dataclass
class WarehouseValuation:
    purchase_cost: Decimal  # закупочная стоимость склада, ₽
    sale_value: Decimal  # оценка склада по цене продажи, ₽
    potential_profit: Decimal  # оценка продажи − закупка, ₽
    unpriced_positions: int  # позиций без закупочной цены
    unpriced_units: Decimal  # единиц без закупочной цены
    usd_rate: Decimal  # курс закупочной оценки (настройка)


def _physical_by_part() -> dict[int, Decimal]:
    """Физический остаток по видам деталей: лоты + поштучные экземпляры.

    Те же статусы «физически на складе», что и в остатках (Слой 10):
    проданное/списанное/выданное уже исключено самой физикой остатков.
    """
    qty: dict[int, Decimal] = {}
    lots = (
        StockLot.objects.filter(status__in=LOT_PHYSICAL_STATUSES, quantity__gt=0)
        .values("part_type_id")
        .annotate(q=Sum("quantity"))
    )
    for row in lots:
        qty[row["part_type_id"]] = qty.get(row["part_type_id"], DEC0) + (row["q"] or DEC0)
    items = (
        PartItem.objects.filter(status__in=ITEM_PHYSICAL_STATUSES)
        .values("part_type_id")
        .annotate(q=Count("pk"))
    )
    for row in items:
        qty[row["part_type_id"]] = qty.get(row["part_type_id"], DEC0) + Decimal(row["q"])
    return {pk: q for pk, q in qty.items() if q > 0}


def _polaris_wholesale_usd(polaris: PolarisCatalogPart) -> Decimal | None:
    """Оптовая цена Polaris: сама позиция, иначе superseded-связь с оптовой > 0.

    Identity детали не меняется — ищется только ЦЕНА (та же безопасная схема
    связей, что у find_polaris_price_source, но по колонке «ОПТОВАЯ»).
    """
    if polaris.wholesale_price_usd is not None and polaris.wholesale_price_usd > 0:
        return polaris.wholesale_price_usd
    related = Q()
    if polaris.part_number_norm:
        related |= Q(superseded_number_norm=polaris.part_number_norm)
    if polaris.superseded_number_norm:
        related |= Q(part_number_norm=polaris.superseded_number_norm)
    if not related:
        return None
    source = (
        PolarisCatalogPart.objects.filter(related, wholesale_price_usd__gt=0)
        .order_by("pk")
        .first()
    )
    return source.wholesale_price_usd if source else None


def _brp_base_usd(brp) -> Decimal | None:
    """Базовая долларовая цена BRP (до наценки): exact, иначе replacement-источник."""
    source = find_brp_price_source(brp.material_no_norm, brp)
    if source is not None and source.retail_price_usd and source.retail_price_usd > 0:
        return source.retail_price_usd
    return None


def get_warehouse_valuation() -> WarehouseValuation:
    """Посчитать три показателя по текущему физическому остатку (read-only)."""
    valuation_settings = ValuationSettings.get()
    rate = valuation_settings.current_usd_rate
    qty_by_part = _physical_by_part()
    purchase_total = DEC0
    sale_total = DEC0
    unpriced_positions = 0
    unpriced_units = DEC0

    # Настройки клиентских цен читаются ОДИН раз на расчёт (иначе N+1:
    # current_customer_price_rub перечитывает их для каждой детали).
    brp_settings = BrpPricingSettings.get()
    polaris_settings = PolarisPricingSettings.get()

    parts = PartType.objects.filter(pk__in=qty_by_part).select_related(
        "brp_link__brp_part", "polaris_link__polaris_part"
    )
    for part in parts:
        qty = qty_by_part[part.pk]
        base_usd = None
        sale_rub = None
        brp_link = getattr(part, "brp_link", None)
        polaris_link = getattr(part, "polaris_link", None)
        if brp_link is not None:
            brp = brp_link.brp_part
            base_usd = _brp_base_usd(brp)
            if base_usd is not None:
                # Источник продажи тот же, что и закупки (retail-база BRP);
                # формула клиентской цены — существующая (pricing BRP).
                sale_rub = brp_customer_price_rub(
                    base_usd, rate, brp_settings.brp_markup_percent
                )
        elif polaris_link is not None:
            polaris = polaris_link.polaris_part
            base_usd = _polaris_wholesale_usd(polaris)
            retail_source = find_polaris_price_source(polaris.part_number_norm, polaris)
            if retail_source is not None and retail_source.retail_price_usd:
                sale_rub = polaris_customer_price_rub(
                    retail_source.retail_price_usd,
                    rate,
                    polaris_settings.polaris_markup_percent,
                )
        if sale_rub is None:
            # Ручные карточки и позиции без каталожной цены: действующая
            # клиентская цена самой карточки.
            sale_rub = part.recommended_price

        if sale_rub:
            sale_total += qty * sale_rub
        if base_usd is not None and base_usd > 0:
            purchase_total += qty * base_usd * rate
        else:
            unpriced_positions += 1
            unpriced_units += qty

    purchase_total = money(purchase_total)
    sale_total = money(sale_total)
    return WarehouseValuation(
        purchase_cost=purchase_total,
        sale_value=sale_total,
        potential_profit=money(sale_total - purchase_total),
        unpriced_positions=unpriced_positions,
        unpriced_units=unpriced_units,
        usd_rate=rate,
    )
