from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from apps.brp.models import BrpPartLink, BrpPricingSettings
from apps.brp.pricing import customer_price_rub as brp_customer_price_rub
from apps.counting.services import find_brp_price_source
from apps.polaris.models import PolarisPartLink, PolarisPricingSettings
from apps.polaris.pricing import customer_price_rub as polaris_customer_price_rub
from apps.polaris.services import find_polaris_price_source
from apps.procurement.models import money
from apps.warehouse.models import ValuationSettings


@dataclass(frozen=True)
class CurrentPriceSettings:
    current_usd_rate: Decimal
    brp_markup_percent: Decimal
    polaris_markup_percent: Decimal
    updated_at: object
    updated_by: object | None


def get_current_price_settings() -> CurrentPriceSettings:
    valuation = ValuationSettings.get()
    brp = BrpPricingSettings.get()
    polaris = PolarisPricingSettings.get()
    return CurrentPriceSettings(
        current_usd_rate=valuation.current_usd_rate,
        brp_markup_percent=brp.brp_markup_percent,
        polaris_markup_percent=polaris.polaris_markup_percent,
        updated_at=valuation.updated_at,
        updated_by=valuation.updated_by,
    )


def _locked_singleton(model):
    obj = model.objects.select_for_update().filter(pk=1).first()
    if obj is None:
        obj = model.objects.create(pk=1)
    return obj


def _brp_link_price(link: BrpPartLink, usd_rate: Decimal, markup: Decimal):
    source = find_brp_price_source(link.brp_part.material_no_norm, link.brp_part)
    if source is None:
        return None
    return brp_customer_price_rub(source.retail_price_usd, usd_rate, markup)


def _polaris_link_price(link: PolarisPartLink, usd_rate: Decimal, markup: Decimal):
    source = find_polaris_price_source(link.polaris_part.part_number_norm, link.polaris_part)
    if source is None:
        return None
    return polaris_customer_price_rub(source.retail_price_usd, usd_rate, markup)


def refresh_linked_part_prices(
    *, usd_rate: Decimal, brp_markup: Decimal, polaris_markup: Decimal
) -> int:
    """Update current PartType recommended prices without changing historical snapshots."""
    parts_to_update = {}

    brp_links = BrpPartLink.objects.select_related("brp_part", "part").filter(
        price_source=BrpPartLink.PriceSource.CALCULATED
    )
    for link in brp_links:
        price = _brp_link_price(link, usd_rate, brp_markup)
        recommended = money(price) if price is not None else None
        if link.part.recommended_price != recommended:
            link.part.recommended_price = recommended
            parts_to_update[link.part_id] = link.part

    polaris_links = PolarisPartLink.objects.select_related("polaris_part", "part").filter(
        price_source=PolarisPartLink.PriceSource.CALCULATED
    )
    for link in polaris_links:
        price = _polaris_link_price(link, usd_rate, polaris_markup)
        recommended = money(price) if price is not None else None
        if link.part.recommended_price != recommended:
            link.part.recommended_price = recommended
            parts_to_update[link.part_id] = link.part

    if parts_to_update:
        from apps.catalog.models import PartType

        PartType.objects.bulk_update(parts_to_update.values(), ["recommended_price"])
    return len(parts_to_update)


@transaction.atomic
def update_current_price_settings(
    *,
    current_usd_rate: Decimal,
    brp_markup_percent: Decimal,
    polaris_markup_percent: Decimal,
    by=None,
) -> tuple[CurrentPriceSettings, int]:
    valuation = _locked_singleton(ValuationSettings)
    brp = _locked_singleton(BrpPricingSettings)
    polaris = _locked_singleton(PolarisPricingSettings)

    valuation.current_usd_rate = current_usd_rate
    valuation.updated_by = by
    valuation.save(update_fields=["current_usd_rate", "updated_by", "updated_at"])

    brp.brp_markup_percent = brp_markup_percent
    brp.updated_by = by
    brp.save(update_fields=["brp_markup_percent", "updated_by", "updated_at"])

    polaris.polaris_markup_percent = polaris_markup_percent
    polaris.updated_by = by
    polaris.save(update_fields=["polaris_markup_percent", "updated_by", "updated_at"])

    refreshed = refresh_linked_part_prices(
        usd_rate=current_usd_rate,
        brp_markup=brp_markup_percent,
        polaris_markup=polaris_markup_percent,
    )
    return get_current_price_settings(), refreshed
