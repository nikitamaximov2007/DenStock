"""Polaris catalog lookup and promotion services."""
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When

from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.procurement.models import money
from apps.receipts.models import Receipt
from apps.receipts.services import create_receipt
from apps.suppliers.models import Supplier
from apps.warehouse.models import ValuationSettings

from .models import PolarisCatalogPart, PolarisPartLink, PolarisPricingSettings
from .pricing import current_customer_price_rub, customer_price_rub

POLARIS_CATEGORY_NAME = "POLARIS"
DEFAULT_UNIT_NAME = "Штука"
INTAKE_SUPPLIER_NAME = "Стартовый ввод"
INTAKE_COMMENT = "Инвентаризация начальных остатков (Polaris)"


class PolarisPromotionError(Exception):
    """Polaris row cannot be promoted to a warehouse card."""


def _default_unit() -> Unit:
    unit = Unit.objects.filter(name__iexact=DEFAULT_UNIT_NAME, is_active=True).first()
    if unit is None:
        unit = Unit.objects.filter(is_active=True).first()
    if unit is None:
        raise PolarisPromotionError("В справочниках нет единиц измерения: добавьте хотя бы одну.")
    return unit


def find_polaris_by_number(norm: str) -> PolarisCatalogPart | None:
    """Find by exact part_number first, then by superseded_number."""
    if not norm:
        return None
    return (
        PolarisCatalogPart.objects.filter(
            Q(part_number_norm=norm) | Q(superseded_number_norm=norm)
        )
        .order_by(
            Case(
                When(part_number_norm=norm, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            Case(
                When(retail_price_usd__gt=0, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            "pk",
        )
        .first()
    )


def find_polaris_price_source(
    norm: str,
    selected: PolarisCatalogPart | None,
    *,
    candidates=None,
) -> PolarisCatalogPart | None:
    """Price source can differ from identity, but identity is never replaced."""
    if selected is not None and (
        selected.retail_price_usd is not None and selected.retail_price_usd > 0
    ):
        return selected
    if not norm and selected is None:
        return None
    related = Q()
    if norm:
        related |= Q(part_number_norm=norm) | Q(superseded_number_norm=norm)
    if selected is not None:
        if selected.superseded_number_norm:
            related |= Q(part_number_norm=selected.superseded_number_norm)
        if selected.part_number_norm:
            related |= Q(superseded_number_norm=selected.part_number_norm)
    if not related:
        return selected
    if candidates is None:
        priced = (
            PolarisCatalogPart.objects.filter(related, retail_price_usd__gt=0)
            .order_by("pk")
            .first()
        )
    else:
        superseded_norm = selected.superseded_number_norm if selected is not None else ""
        priced = next(
            (
                candidate
                for candidate in candidates
                if candidate.retail_price_usd is not None
                and candidate.retail_price_usd > 0
                and (
                    candidate.part_number_norm == norm
                    or candidate.superseded_number_norm == norm
                    or candidate.part_number_norm == superseded_norm
                )
            ),
            None,
        )
    return priced or selected


def load_polaris_price_candidates(selected_parts) -> list[PolarisCatalogPart]:
    """Одним запросом загрузить retail/wholesale candidates связанных позиций."""
    selected_parts = list(selected_parts)
    exact_norms = {part.part_number_norm for part in selected_parts if part.part_number_norm}
    superseded_norms = {
        part.superseded_number_norm
        for part in selected_parts
        if part.superseded_number_norm
    }
    if not exact_norms and not superseded_norms:
        return []
    return list(
        PolarisCatalogPart.objects.filter(
            Q(part_number_norm__in=exact_norms | superseded_norms)
            | Q(superseded_number_norm__in=exact_norms)
        )
        .filter(Q(retail_price_usd__gt=0) | Q(wholesale_price_usd__gt=0))
        .order_by("pk")
    )


def effective_customer_price_rub(norm: str, polaris: PolarisCatalogPart):
    source = find_polaris_price_source(norm, polaris)
    if source is None:
        return None
    return current_customer_price_rub(source.retail_price_usd)


@transaction.atomic
def promote_to_warehouse(
    polaris_part: PolarisCatalogPart, *, by=None, manual_price=None
) -> PartType:
    """Create a warehouse card from Polaris reference data. Does not create stock."""
    existing = (
        PolarisPartLink.objects.filter(polaris_part=polaris_part)
        .select_related("part")
        .first()
    )
    if existing is not None:
        return existing.part

    valuation = ValuationSettings.get()
    settings = PolarisPricingSettings.get()
    calculated = customer_price_rub(
        polaris_part.retail_price_usd,
        valuation.current_usd_rate,
        settings.polaris_markup_percent,
    )
    final = manual_price if manual_price is not None else calculated
    source = (
        PolarisPartLink.PriceSource.MANUAL
        if manual_price is not None
        else PolarisPartLink.PriceSource.CALCULATED
    )

    category, _ = Category.objects.get_or_create(
        name=POLARIS_CATEGORY_NAME, parent=None, defaults={"sort_order": 0}
    )
    manufacturer, _ = Manufacturer.objects.get_or_create(name=POLARIS_CATEGORY_NAME)
    name = polaris_part.part_name or f"POLARIS {polaris_part.part_number}"
    part = PartType.objects.create(
        name=name[:200],
        category=category,
        manufacturer=manufacturer,
        unit=_default_unit(),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=money(final) if final is not None else None,
        description=f"Из Polaris-каталога, номер {polaris_part.part_number}.",
    )
    PartNumber.objects.create(
        part=part, value=polaris_part.part_number,
        kind=PartNumber.Kind.OEM, is_primary=True,
    )
    if (
        polaris_part.superseded_number
        and polaris_part.superseded_number != polaris_part.part_number
    ):
        PartNumber.objects.create(
            part=part, value=polaris_part.superseded_number, kind=PartNumber.Kind.ANALOG
        )

    PolarisPartLink.objects.create(
        part=part,
        polaris_part=polaris_part,
        polaris_retail_price_usd=polaris_part.retail_price_usd,
        polaris_wholesale_price_usd=polaris_part.wholesale_price_usd,
        usd_rate_used=valuation.current_usd_rate,
        markup_percent_used=settings.polaris_markup_percent,
        calculated_customer_price_rub=calculated,
        manual_customer_price_rub=manual_price,
        final_customer_price_rub=final,
        price_source=source,
        created_by=by,
    )
    return part


def find_promoted_part(polaris_part: PolarisCatalogPart):
    link = (
        PolarisPartLink.objects.filter(polaris_part=polaris_part)
        .select_related("part")
        .first()
    )
    return link.part if link else None


def get_or_create_intake_draft(*, by) -> Receipt:
    draft = (
        Receipt.objects.filter(
            status=Receipt.Status.DRAFT,
            created_by=by,
            comment__startswith=INTAKE_COMMENT,
        )
        .order_by("-created_at")
        .first()
    )
    if draft is not None:
        return draft
    supplier, _ = Supplier.objects.get_or_create(
        name=INTAKE_SUPPLIER_NAME, defaults={"is_active": True}
    )
    return create_receipt(supplier=supplier, comment=INTAKE_COMMENT, by=by)

