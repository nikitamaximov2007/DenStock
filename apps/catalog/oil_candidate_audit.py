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

    def asdict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OilCandidateReport:
    already_oil_count: int
    candidate_count: int
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

    rows = [
        OilCandidateRow(
            part_type_id=part_id,
            name=parts[part_id].name,
            manufacturer=(
                parts[part_id].manufacturer.name if parts[part_id].manufacturer_id else ""
            ),
            category=parts[part_id].category.name if parts[part_id].category_id else "",
            tracking_mode=parts[part_id].tracking_mode,
            matched_numbers=", ".join(sorted(set(numbers))),
            reason=f"article starts with {OIL_ARTICLE_PREFIX!r} (MOTUL oil convention, unverified)",
        )
        for part_id, numbers in sorted(by_part.items())
    ]

    return OilCandidateReport(
        already_oil_count=already_oil_count,
        candidate_count=len(rows),
        rows=rows,
    )
