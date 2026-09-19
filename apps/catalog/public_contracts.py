"""Read-only contracts shared by internal search and the future public catalog.

The module is intentionally narrow: it projects current customer price,
available quantity, and public-safe identity data without recreating pricing
or inventory business rules.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from apps.actions.models import PartCustomsInfo
from apps.inventory.availability import available_totals
from apps.inventory.presentation import manufacturer_display, part_exact_number, with_part_identity
from apps.inventory.pricing import (
    protected_customer_price_floors,
    resolve_effective_part_customer_price,
)

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

    public_id: UUID
    article: str
    english_name: str
    russian_name: str | None
    manufacturer: str
    unit: PublicUnit
    price: CurrentCustomerPrice
    available_quantity: Decimal


_FETCH_FLOOR = object()


def resolve_current_customer_price(
    part: PartType, *, protected_floor: Decimal | None | object = _FETCH_FLOOR
) -> CurrentCustomerPrice:
    """Read a public-safe current price from the canonical price result.

    Pricing pipelines own all calculations and updates. The facade only
    exposes a finite positive Decimal as a known price; every other state is
    deliberately represented as ``clarify``.

    A known price never falls below the protected customer price of stock
    still in the warehouse (``apps.inventory.pricing``): the result is the
    higher of the two.  The floor only raises a certified price; it never
    turns an unverified or missing price into a public number.

    ``protected_floor`` is for bulk callers that already asked
    ``protected_customer_price_floors``; omitted, it is read for this part.
    Many parts at once belong to ``resolve_current_customer_prices``.
    """
    price = part.recommended_price
    formula_certified = (
        part.price_provenance == PartType.PriceProvenance.FORMULA_CERTIFIED
        and part.certified_price_rub == price
    )
    valid_manual_exception = (
        part.price_provenance == PartType.PriceProvenance.VALID_MANUAL_EXCEPTION
    )
    if (
        isinstance(price, Decimal)
        and price.is_finite()
        and price > ZERO
        and (formula_certified or valid_manual_exception)
    ):
        if protected_floor is _FETCH_FLOOR:
            protected_floor = protected_customer_price_floors([part.pk]).get(part.pk)
        effective = resolve_effective_part_customer_price(price, protected_floor)
        return CurrentCustomerPrice(price_rub=effective, status="known")
    return CurrentCustomerPrice(price_rub=None, status="clarify")


def resolve_current_customer_prices(
    parts: Iterable[PartType],
) -> dict[int, CurrentCustomerPrice]:
    """``resolve_current_customer_price`` for many parts with one floor lookup."""
    parts = list(parts)
    floors = protected_customer_price_floors(part.pk for part in parts)
    return {
        part.pk: resolve_current_customer_price(part, protected_floor=floors.get(part.pk))
        for part in parts
    }


def build_public_part_facts(
    part_ids: Iterable[int], *, quantities: Mapping[int, Decimal] | None = None
) -> list[PublicPartFacts]:
    """Hydrate public-safe facts for many part IDs without per-part queries.

    Result order follows the requested IDs. Unknown IDs are omitted. The only
    Russian name included is a nonblank value that an operator explicitly
    confirmed in ``PartCustomsInfo``.

    ``quantities`` lets a caller that already asked ``available_totals`` for a
    superset of these IDs in the same request reuse that answer instead of
    reading stock twice. It must come from ``available_totals``; any ID it
    lacks is read fresh, so a partial mapping cannot report a false zero.
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
    known = quantities or {}
    missing = [part_id for part_id in parts_by_id if part_id not in known]
    quantities = {**known, **available_totals(missing)} if missing else known
    prices = resolve_current_customer_prices(parts)

    return [
        PublicPartFacts(
            public_id=part.public_id,
            article=part_exact_number(part, default=""),
            english_name=part.name,
            russian_name=russian_names.get(part.pk),
            manufacturer=manufacturer_display(part),
            unit=PublicUnit(name=part.unit.name, short_name=part.unit.short_name),
            price=prices[part.pk],
            available_quantity=quantities[part.pk],
        )
        for part_id in ids
        if (part := parts_by_id.get(part_id)) is not None
    ]
