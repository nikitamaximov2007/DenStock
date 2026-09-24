"""Read-only audit of the "Себестоимость" cost basis behind completed sales.

`get_sales_report` (apps/reports/services.py) treats a SaleLine as having a
KNOWN cost only when `unmarked_unit_price_rub_snapshot` is set. This module
answers, without changing anything, *why* a line does or does not have that
base: captured live at sale time, reconstructed once via the owner-approved
legacy 105 ₽/USD backfill (migration 0007 - still valid, never invalidated
just for being "105"/fixed), or genuinely unknown.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from apps.sales.models import Sale, SaleLine

from .services import sale_line_amount_for_quantity

LEGACY_NOTE_PREFIX = "legacy_reconstruction_"


@dataclass(frozen=True)
class CostProvenanceRow:
    sale_id: int
    sale_number: str
    sold_at: object
    line_id: int
    part_type_id: int
    part_name: str
    quantity: Decimal
    unit_price_rub: Decimal
    provenance: str  # "live" | "legacy_105" | "unknown"
    unmarked_price_source: str
    unmarked_unit_price_rub_snapshot: Decimal | None
    unmarked_usd_rate_snapshot: Decimal | None

    def asdict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CostProvenanceReport:
    total_lines: int
    live_count: int
    legacy_105_count: int
    unknown_count: int
    live_known_revenue: Decimal
    legacy_105_known_revenue: Decimal
    unknown_revenue: Decimal
    by_source: dict
    rows: list


def _provenance(line: SaleLine) -> str:
    if line.unmarked_unit_price_rub_snapshot is None:
        return "unknown"
    note = line.unmarked_price_snapshot_note or ""
    if note.startswith(LEGACY_NOTE_PREFIX):
        return "legacy_105"
    return "live"


def audit_sale_cost_provenance() -> CostProvenanceReport:
    """Classify every completed SaleLine's cost-basis provenance. Read-only."""
    lines = (
        SaleLine.objects.filter(sale__status=Sale.Status.COMPLETED)
        .select_related("sale", "part_type")
        .order_by("sale__sold_at", "sale_id", "id")
    )
    rows: list[CostProvenanceRow] = []
    by_source: dict[str, int] = {}
    live_known_revenue = Decimal("0")
    legacy_105_known_revenue = Decimal("0")
    unknown_revenue = Decimal("0")
    live_count = legacy_105_count = unknown_count = 0

    for line in lines:
        provenance = _provenance(line)
        revenue = sale_line_amount_for_quantity(line)
        source_key = line.unmarked_price_source or "-"
        by_source[source_key] = by_source.get(source_key, 0) + 1
        if provenance == "live":
            live_count += 1
            live_known_revenue += revenue
        elif provenance == "legacy_105":
            legacy_105_count += 1
            legacy_105_known_revenue += revenue
        else:
            unknown_count += 1
            unknown_revenue += revenue
        rows.append(
            CostProvenanceRow(
                sale_id=line.sale_id,
                sale_number=line.sale.number,
                sold_at=line.sale.sold_at,
                line_id=line.id,
                part_type_id=line.part_type_id,
                part_name=line.part_type.name,
                quantity=line.quantity,
                unit_price_rub=line.unit_price,
                provenance=provenance,
                unmarked_price_source=line.unmarked_price_source or "",
                unmarked_unit_price_rub_snapshot=line.unmarked_unit_price_rub_snapshot,
                unmarked_usd_rate_snapshot=line.unmarked_usd_rate_snapshot,
            )
        )

    return CostProvenanceReport(
        total_lines=len(rows),
        live_count=live_count,
        legacy_105_count=legacy_105_count,
        unknown_count=unknown_count,
        live_known_revenue=live_known_revenue,
        legacy_105_known_revenue=legacy_105_known_revenue,
        unknown_revenue=unknown_revenue,
        by_source=by_source,
        rows=rows,
    )
