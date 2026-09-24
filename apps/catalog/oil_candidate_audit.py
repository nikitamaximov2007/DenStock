"""Read-only audit for parts that MIGHT be oil (масло) but are not marked so.

This is a review aid, not a classification rule. The "337..." article prefix
is the MOTUL numbering convention cited by the business as a hint - it is
never used to auto-mark a part as oil anywhere in this codebase. A human
reviews each candidate and, if it really is oil, edits the PartType via the
normal form (is_oil + oil_package_volume_l) themselves.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from apps.catalog.models import PartNumber, PartType

# MOTUL's own numbering convention for packaged oil/lubricant products.
# Cited in the business's own catalog as a signal, never as ground truth:
# a "337..." article could still be a non-oil accessory, and a real oil
# part might use a different supplier's numbering entirely.
OIL_ARTICLE_PREFIX = "337"


@dataclass(frozen=True)
class OilCandidateRow:
    part_type_id: int
    name: str
    manufacturer: str
    category: str
    tracking_mode: str
    matched_numbers: str
    reason: str
    # "safe_to_mark": no stock/movements/sales/repairs yet - PartType.clean()
    # would allow flipping is_oil today. "needs_owner_review": history already
    # exists, so the immutability guard would block a naive flip - converting
    # this one (if it truly is oil) needs a deliberate decision, not a click.
    candidate_status: str

    def asdict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OilCandidateReport:
    already_oil_count: int
    candidate_count: int
    safe_to_mark_count: int
    needs_owner_review_count: int
    rows: list


def audit_oil_candidates() -> OilCandidateReport:
    """List non-oil PartTypes whose article numbers look like MOTUL oil SKUs.

    Read-only: never writes to PartType.is_oil. The caller (a human, via the
    normal edit form) decides.
    """
    already_oil_count = PartType.objects.filter(is_oil=True).count()

    candidate_numbers = (
        PartNumber.objects.filter(
            normalized_value__startswith=OIL_ARTICLE_PREFIX,
            part__is_oil=False,
        )
        .select_related("part", "part__manufacturer", "part__category")
        .order_by("part_id", "value")
    )

    by_part: dict[int, list[str]] = {}
    parts: dict[int, PartType] = {}
    for number in candidate_numbers:
        by_part.setdefault(number.part_id, []).append(number.value)
        parts[number.part_id] = number.part

    rows = []
    safe_to_mark_count = 0
    needs_owner_review_count = 0
    for part_id, numbers in sorted(by_part.items()):
        part = parts[part_id]
        has_history = part.has_stock_or_history()
        status = "needs_owner_review" if has_history else "safe_to_mark"
        if has_history:
            needs_owner_review_count += 1
        else:
            safe_to_mark_count += 1
        rows.append(
            OilCandidateRow(
                part_type_id=part_id,
                name=part.name,
                manufacturer=part.manufacturer.name if part.manufacturer_id else "",
                category=part.category.name if part.category_id else "",
                tracking_mode=part.tracking_mode,
                matched_numbers=", ".join(sorted(set(numbers))),
                reason=(
                    f"article starts with {OIL_ARTICLE_PREFIX!r} "
                    "(MOTUL oil convention, unverified)"
                ),
                candidate_status=status,
            )
        )

    return OilCandidateReport(
        already_oil_count=already_oil_count,
        candidate_count=len(rows),
        safe_to_mark_count=safe_to_mark_count,
        needs_owner_review_count=needs_owner_review_count,
        rows=rows,
    )


@dataclass(frozen=True)
class OilPartStatusRow:
    """One already-marked oil PartType's configuration and real usage."""

    part_type_id: int
    name: str
    manufacturer: str
    oil_package_volume_l: str
    available_liters: str
    stock_lot_count: int
    movement_count: int
    sale_line_count: int
    repair_line_count: int
    tracking_mode: str
    configuration_status: str  # "ok" | "missing_package_volume" | "wrong_tracking_mode"


@dataclass(frozen=True)
class OilMigrationReadinessReport:
    """Explicit oil parts: configuration + how much real history already exists.

    Read-only. Answers "is this oil part safely configured, and how much
    history already depends on its current settings" - not "should this
    part be oil", which stays a human/business decision made elsewhere.
    """

    rows: list


def audit_oil_migration_readiness() -> OilMigrationReadinessReport:
    from apps.inventory.models import StockLot, StockMovement
    from apps.inventory.pricing import oil_availability_rows
    from apps.repairs.models import RepairIssueLine
    from apps.sales.models import SaleLine

    parts = list(
        PartType.objects.filter(is_oil=True).select_related("manufacturer").order_by("name")
    )
    if not parts:
        return OilMigrationReadinessReport(rows=[])

    availability = {row.part_type_id: row for row in oil_availability_rows(parts)}
    part_ids = [part.pk for part in parts]
    lot_counts = _count_by_part(StockLot.objects.filter(part_type_id__in=part_ids), part_ids)
    movement_counts = _count_by_part(
        StockMovement.objects.filter(part_type_id__in=part_ids), part_ids
    )
    sale_counts = _count_by_part(SaleLine.objects.filter(part_type_id__in=part_ids), part_ids)
    repair_counts = _count_by_part(
        RepairIssueLine.objects.filter(part_type_id__in=part_ids), part_ids
    )

    rows = []
    for part in parts:
        if part.tracking_mode != PartType.TrackingMode.BULK:
            status = "wrong_tracking_mode"
        elif not part.oil_package_volume_l or part.oil_package_volume_l <= 0:
            status = "missing_package_volume"
        else:
            status = "ok"
        row = availability.get(part.pk)
        rows.append(
            OilPartStatusRow(
                part_type_id=part.pk,
                name=part.name,
                manufacturer=part.manufacturer.name if part.manufacturer_id else "",
                oil_package_volume_l=(
                    str(part.oil_package_volume_l) if part.oil_package_volume_l else ""
                ),
                available_liters=str(row.available_l) if row else "0",
                stock_lot_count=lot_counts.get(part.pk, 0),
                movement_count=movement_counts.get(part.pk, 0),
                sale_line_count=sale_counts.get(part.pk, 0),
                repair_line_count=repair_counts.get(part.pk, 0),
                tracking_mode=part.tracking_mode,
                configuration_status=status,
            )
        )
    return OilMigrationReadinessReport(rows=rows)


def _count_by_part(queryset, part_ids) -> dict[int, int]:
    from django.db.models import Count

    counts = dict(
        queryset.values_list("part_type_id")
        .annotate(n=Count("id"))
        .values_list("part_type_id", "n")
    )
    return {part_id: counts.get(part_id, 0) for part_id in part_ids}
