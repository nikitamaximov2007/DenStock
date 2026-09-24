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
from apps.inventory.pricing import effective_part_customer_prices

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
    # Масло: available_quantity выше уже в литрах (физический остаток), а
    # `unit`/цена остаются пакетными (V1: публичный запрос - по упаковкам,
    # см. docs). is_oil даёт шаблону показать литры наличия отдельной
    # подписью, не путая её с единицей запроса/цены.
    is_oil: bool = False
    oil_package_volume_l: Decimal | None = None


PRICE_PARITY_A = "A"
PRICE_PARITY_B = "B"
PRICE_PARITY_C = "C"
PRICE_PARITY_D = "D"
PRICE_PARITY_E = "E"
PRICE_PARITY_CATEGORIES = (
    PRICE_PARITY_A,
    PRICE_PARITY_B,
    PRICE_PARITY_C,
    PRICE_PARITY_D,
    PRICE_PARITY_E,
)


@dataclass(frozen=True, slots=True)
class PublicPriceParityRow:
    """One read-only comparison between internal and public current price."""

    part_id: int
    internal_price: Decimal | None
    public_price: Decimal | None
    category: str


@dataclass(frozen=True, slots=True)
class PublicPriceParityAudit:
    """A bounded, read-only audit of public/sellable PartTypes."""

    rows: tuple[PublicPriceParityRow, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            category: sum(row.category == category for row in self.rows)
            for category in PRICE_PARITY_CATEGORIES
        }


def _public_price_from_internal(price: Decimal | None) -> CurrentCustomerPrice:
    if price is not None and price > ZERO:
        return CurrentCustomerPrice(price_rub=price, status="known")
    return CurrentCustomerPrice(price_rub=None, status="clarify")


def resolve_current_customer_price(part: PartType) -> CurrentCustomerPrice:
    """Project DenisStock's current customer price without re-validating it.

    ``recommended_price`` is resolved by the same inventory pricing service
    used by DenisStock's internal screens. ``certified_price_rub`` and
    ``price_provenance`` remain audit metadata; disagreement with them must
    not hide a positive current price accepted and displayed internally.
    """
    price = effective_part_customer_prices([part]).get(part.pk)
    return _public_price_from_internal(price)


def resolve_current_customer_prices(
    parts: Iterable[PartType],
) -> dict[int, CurrentCustomerPrice]:
    """Resolve the authoritative current price for many parts."""
    parts = list(parts)
    prices = effective_part_customer_prices(parts)
    return {
        part.pk: _public_price_from_internal(prices.get(part.pk))
        for part in parts
    }


def audit_public_price_parity(parts: Iterable[PartType]) -> PublicPriceParityAudit:
    """Compare public/sellable prices with DenisStock's current price.

    Categories are A/B/C/D/E from the release acceptance contract. The audit
    performs no writes and uses the same current-price resolver as internal
    screens and public facts.
    """
    parts = list(parts)
    internal_prices = effective_part_customer_prices(parts)
    public_prices = resolve_current_customer_prices(parts)
    rows = []
    for part in parts:
        internal = internal_prices.get(part.pk)
        public = public_prices[part.pk].price_rub
        if internal is not None and public == internal:
            category = PRICE_PARITY_A
        elif internal is not None and public is None:
            category = PRICE_PARITY_B
        elif internal is not None:
            category = PRICE_PARITY_C
        elif public is None:
            category = PRICE_PARITY_D
        else:
            category = PRICE_PARITY_E
        rows.append(
            PublicPriceParityRow(
                part_id=part.pk,
                internal_price=internal,
                public_price=public,
                category=category,
            )
        )
    return PublicPriceParityAudit(rows=tuple(rows))


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
            # ВАЖНО: это единица ЗАПРОСА/цены (для масла - "упаковка", V1
            # держит публичный запрос пакетным - см. apps.catalog.quantity_units
            # и docs/ai-support про CustomerRequest→Sale), а НЕ единица
            # физического наличия. available_quantity ниже - всегда литры для
            # масла независимо от этого поля; шаблон показывает их отдельно.
            unit=PublicUnit(name=part.unit.name, short_name=part.unit.short_name),
            price=prices[part.pk],
            available_quantity=quantities[part.pk],
            is_oil=part.is_oil,
            oil_package_volume_l=part.oil_package_volume_l,
        )
        for part_id in ids
        if (part := parts_by_id.get(part_id)) is not None
    ]
