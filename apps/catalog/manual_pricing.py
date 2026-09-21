"""Explicit pricing for manual catalog cards with a RUB purchase source."""

from decimal import Decimal

from django.db import transaction

from apps.procurement.models import money

from .models import ManualPurchasePrice, PartType

MANUAL_CUSTOMER_MARKUP = Decimal("0.40")


def customer_price_from_purchase_price(purchase_price_rub: Decimal) -> Decimal:
    """Return the current customer price for a manual purchase-price source."""
    return money(Decimal(purchase_price_rub) * (Decimal("1") + MANUAL_CUSTOMER_MARKUP))


@transaction.atomic
def set_manual_purchase_price(
    part: PartType, purchase_price_rub: Decimal
) -> PartType:
    """Store a manual purchase price and synchronise its customer price.

    This is an explicit operation. ``PartType.save()`` does not infer or
    silently recalculate prices, while this service keeps the two prices
    together when an authorised caller changes the purchase source. Historical
    ``SaleLine`` rows are not touched.
    """
    purchase_price = money(purchase_price_rub)
    if purchase_price <= 0:
        raise ValueError("Закупочная цена должна быть положительной.")

    ManualPurchasePrice.objects.update_or_create(
        part_type=part,
        defaults={"purchase_price_rub": purchase_price},
    )
    part.recommended_price = customer_price_from_purchase_price(purchase_price)
    part.certified_price_rub = None
    part.price_provenance = PartType.PriceProvenance.VALID_MANUAL_EXCEPTION
    part.save(
        update_fields=[
            "recommended_price",
            "certified_price_rub",
            "price_provenance",
        ]
    )
    return part
