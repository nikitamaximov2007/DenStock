"""Read-only contracts shared by internal search and the future public catalog.

The module is intentionally narrow: it projects current customer price,
available quantity, and public-safe identity data without recreating pricing
or inventory business rules.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from apps.actions.models import PartCustomsInfo
from apps.inventory.availability import available_totals
from apps.inventory.presentation import manufacturer_display, part_exact_number, with_part_identity

from .models import PartType

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class CurrentCustomerPrice:
    """Current public customer price, or an explicit request to clarify it."""

    price_rub: Decimal | None
    status: Literal["known", "clarify"]


@dataclass(frozen=True, slots=True)
class PublicUnit:
    """Canonical unit data for a public quantity, without presentation assumptions."""

    name: str
    short_name: str


@dataclass(frozen=True, slots=True)
class PublicPartFacts:
    """The deliberately small public-safe projection of a warehouse part."""

    part_id: int
    article: str
    english_name: str
    russian_name: str | None
    manufacturer: str
    unit: PublicUnit
    price: CurrentCustomerPrice
    available_quantity: Decimal


def resolve_current_customer_price(part: PartType) -> CurrentCustomerPrice:
    """Read the authoritative current price from ``PartType.recommended_price``.

    Pricing pipelines own all calculations and updates. The facade only
    exposes a finite positive Decimal as a known price; every other state is
    deliberately represented as ``clarify``.
    """
    price = part.recommended_price
    if isinstance(price, Decimal) and price.is_finite() and price > ZERO:
        return CurrentCustomerPrice(price_rub=price, status="known")
    return CurrentCustomerPrice(price_rub=None, status="clarify")


def build_public_part_facts(part_ids: Iterable[int]) -> list[PublicPartFacts]:
    """Hydrate public-safe facts for many part IDs without per-part queries.

    Result order follows the requested IDs. Unknown IDs are omitted. The only
    Russian name included is a nonblank value that an operator explicitly
    confirmed in ``PartCustomsInfo``.
    """
    ids = list(dict.fromkeys(part_id for part_id in part_ids if part_id is not None))
    if not ids:
        return []

    parts = list(
        with_part_identity(
            PartType.objects.filter(pk__in=ids).select_related("unit"),
            part_field="",
        )
    )
    parts_by_id = {part.pk: part for part in parts}
    russian_names = {
        part_type_id: customs_name_ru.strip()
        for part_type_id, customs_name_ru in PartCustomsInfo.objects.filter(
            part_type_id__in=parts_by_id,
            customs_name_ru_confirmed=True,
        ).values_list("part_type_id", "customs_name_ru")
        if customs_name_ru.strip()
    }
    quantities = available_totals(parts_by_id)

    return [
        PublicPartFacts(
            part_id=part.pk,
            article=part_exact_number(part, default=""),
            english_name=part.name,
            russian_name=russian_names.get(part.pk),
            manufacturer=manufacturer_display(part),
            unit=PublicUnit(name=part.unit.name, short_name=part.unit.short_name),
            price=resolve_current_customer_price(part),
            available_quantity=quantities[part.pk],
        )
        for part_id in ids
        if (part := parts_by_id.get(part_id)) is not None
    ]
