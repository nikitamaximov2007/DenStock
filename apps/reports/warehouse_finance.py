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
from apps.counting.services import find_brp_price_source, load_brp_price_candidates
from apps.inventory.models import PartItem, StockLot
from apps.inventory.services import ITEM_PHYSICAL_STATUSES, LOT_PHYSICAL_STATUSES
from apps.polaris.models import PolarisCatalogPart, PolarisPricingSettings
from apps.polaris.pricing import customer_price_rub as polaris_customer_price_rub
from apps.polaris.services import (
    find_polaris_price_source,
    load_polaris_price_candidates,
)
from apps.procurement.models import money
from apps.warehouse.models import ValuationSettings

DEC0 = Decimal("0")
UNCATEGORIZED_NAME = "Без категории"


@dataclass(frozen=True)
class CategorySaleValue:
    category_id: int | None
    name: str
    quantity: Decimal
    value: Decimal


@dataclass
class WarehouseValuation:
    purchase_cost: Decimal  # закупочная стоимость склада, ₽
    sale_value: Decimal  # оценка склада по цене продажи, ₽
    potential_profit: Decimal  # оценка продажи − закупка, ₽
    unpriced_positions: int  # позиций без закупочной цены
    unpriced_units: Decimal  # единиц без закупочной цены
    sale_unpriced_positions: int  # позиций без клиентской цены
    sale_unpriced_units: Decimal  # единиц без клиентской цены
    sale_by_category: list[CategorySaleValue]
    physical_units: Decimal
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


def _polaris_wholesale_usd(
    polaris: PolarisCatalogPart, candidates=None
) -> Decimal | None:
    """Оптовая цена Polaris: сама позиция, иначе superseded-связь с оптовой > 0.

    Identity детали не меняется — ищется только ЦЕНА (та же безопасная схема
    связей, что у find_polaris_price_source, но по колонке «ОПТОВАЯ»).
    """
    if polaris.wholesale_price_usd is not None and polaris.wholesale_price_usd > 0:
        return polaris.wholesale_price_usd
    if candidates is None:
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
    else:
        source = next(
            (
                candidate
                for candidate in candidates
                if candidate.wholesale_price_usd is not None
                and candidate.wholesale_price_usd > 0
                and (
                    candidate.superseded_number_norm == polaris.part_number_norm
                    or candidate.part_number_norm == polaris.superseded_number_norm
                )
            ),
            None,
        )
    return source.wholesale_price_usd if source else None


def _brp_base_usd(brp, candidates=None) -> Decimal | None:
    """Базовая долларовая цена BRP (до наценки): exact, иначе replacement-источник."""
    source = find_brp_price_source(brp.material_no_norm, brp, candidates=candidates)
    if source is not None and source.retail_price_usd and source.retail_price_usd > 0:
        return source.retail_price_usd
    return None


def _category_identity(part) -> tuple[int | None, str]:
    category_id = getattr(part, "category_id", None)
    category = getattr(part, "category", None)
    return category_id, category.name if category_id is not None else UNCATEGORIZED_NAME


def _sale_price_rub(
    part,
    rate,
    brp_settings,
    polaris_settings,
    brp_candidates,
    polaris_candidates,
) -> Decimal | None:
    """Текущая клиентская цена exact-карточки без изменения её identity."""
    brp_link = getattr(part, "brp_link", None)
    if brp_link is not None:
        if brp_link.price_source == brp_link.PriceSource.MANUAL:
            return part.recommended_price
        base_usd = _brp_base_usd(brp_link.brp_part, brp_candidates)
        if base_usd is not None:
            return brp_customer_price_rub(
                base_usd, rate, brp_settings.brp_markup_percent
            )
        return part.recommended_price

    polaris_link = getattr(part, "polaris_link", None)
    if polaris_link is not None:
        if polaris_link.price_source == polaris_link.PriceSource.MANUAL:
            return part.recommended_price
        polaris = polaris_link.polaris_part
        source = find_polaris_price_source(
            polaris.part_number_norm, polaris, candidates=polaris_candidates
        )
        if source is not None and source.retail_price_usd and source.retail_price_usd > 0:
            return polaris_customer_price_rub(
                source.retail_price_usd,
                rate,
                polaris_settings.polaris_markup_percent,
            )
        return part.recommended_price

    return part.recommended_price


def get_warehouse_valuation() -> WarehouseValuation:
    """Посчитать три показателя по текущему физическому остатку (read-only)."""
    valuation_settings = ValuationSettings.get()
    rate = valuation_settings.current_usd_rate
    qty_by_part = _physical_by_part()
    purchase_total = DEC0
    sale_total = DEC0
    unpriced_positions = 0
    unpriced_units = DEC0
    sale_unpriced_positions = 0
    sale_unpriced_units = DEC0
    category_values: dict[int | None, dict[str, Decimal | str]] = {}

    # Настройки клиентских цен читаются ОДИН раз на расчёт (иначе N+1:
    # current_customer_price_rub перечитывает их для каждой детали).
    brp_settings = BrpPricingSettings.get()
    polaris_settings = PolarisPricingSettings.get()

    parts = list(
        PartType.objects.filter(pk__in=qty_by_part).select_related(
            "category", "brp_link__brp_part", "polaris_link__polaris_part"
        )
    )
    brp_catalog_parts = [
        part.brp_link.brp_part for part in parts if getattr(part, "brp_link", None)
    ]
    polaris_catalog_parts = [
        part.polaris_link.polaris_part
        for part in parts
        if getattr(part, "polaris_link", None)
    ]
    brp_candidates = load_brp_price_candidates(brp_catalog_parts)
    polaris_candidates = load_polaris_price_candidates(polaris_catalog_parts)
    for part in parts:
        qty = qty_by_part[part.pk]
        base_usd = None
        brp_link = getattr(part, "brp_link", None)
        polaris_link = getattr(part, "polaris_link", None)
        if brp_link is not None:
            brp = brp_link.brp_part
            base_usd = _brp_base_usd(brp, brp_candidates)
        elif polaris_link is not None:
            polaris = polaris_link.polaris_part
            base_usd = _polaris_wholesale_usd(polaris, polaris_candidates)

        category_id, category_name = _category_identity(part)
        category = category_values.setdefault(
            category_id,
            {
                "name": category_name,
                "quantity": DEC0,
                "value": DEC0,
            },
        )
        category["quantity"] += qty
        sale_rub = _sale_price_rub(
            part,
            rate,
            brp_settings,
            polaris_settings,
            brp_candidates,
            polaris_candidates,
        )
        if sale_rub is not None and sale_rub > 0:
            category["value"] += qty * sale_rub
        else:
            sale_unpriced_positions += 1
            sale_unpriced_units += qty
        if base_usd is not None and base_usd > 0:
            purchase_total += qty * base_usd * rate
        else:
            unpriced_positions += 1
            unpriced_units += qty

    purchase_total = money(purchase_total)
    sale_by_category = [
        CategorySaleValue(
            category_id=category_id,
            name=str(values["name"]),
            quantity=Decimal(values["quantity"]),
            value=money(Decimal(values["value"])),
        )
        for category_id, values in category_values.items()
    ]
    sale_by_category.sort(key=lambda row: (-row.value, row.name, row.category_id or 0))
    sale_total = sum((row.value for row in sale_by_category), DEC0)
    return WarehouseValuation(
        purchase_cost=purchase_total,
        sale_value=sale_total,
        potential_profit=money(sale_total - purchase_total),
        unpriced_positions=unpriced_positions,
        unpriced_units=unpriced_units,
        sale_unpriced_positions=sale_unpriced_positions,
        sale_unpriced_units=sale_unpriced_units,
        sale_by_category=sale_by_category,
        physical_units=sum(qty_by_part.values(), DEC0),
        usd_rate=rate,
    )
