"""Forensic, fail-closed reconstruction of historical lot price snapshots.

This module intentionally has no automatic caller.  Historical inventory is
not evidence for a customer price.  The sole admissible evidence is the frozen
price in a counting line whose converted receipt can be linked uniquely to the
exact lot and part type.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from apps.counting.models import InventoryCountingLine, InventoryCountingSession
from apps.receipts.models import ReceiptLine

from .models import StockLot


@dataclass(frozen=True)
class HistoricalPriceBackfillRow:
    lot_id: int
    part_type_id: int
    quantity: Decimal
    receipt_id: int | None
    session_id: int | None
    counting_line_id: int | None
    evidence_price: Decimal | None
    outcome: str
    reason: str
    previous_snapshot: Decimal | None


@dataclass(frozen=True)
class HistoricalPriceBackfillPlan:
    rows: tuple[HistoricalPriceBackfillRow, ...]

    @property
    def eligible(self) -> tuple[HistoricalPriceBackfillRow, ...]:
        return tuple(row for row in self.rows if row.outcome == "eligible")

    @property
    def counts(self) -> Counter:
        return Counter(row.outcome for row in self.rows)

    @property
    def quantities(self) -> Counter:
        totals: Counter = Counter()
        for row in self.rows:
            totals[row.outcome] += row.quantity
        return totals


def build_historical_price_backfill_plan(*, lock_lots: bool = False) -> HistoricalPriceBackfillPlan:
    """Build the deterministic plan without changing any inventory data.

    Only current, available bulk lots are in scope.  PartItems deliberately do
    not participate: their relationship cannot be inferred from a bulk count.
    """
    lots_query = StockLot.objects.filter(
        status=StockLot.Status.AVAILABLE, quantity__gt=0
    ).order_by("pk")
    if lock_lots:
        lots_query = lots_query.select_for_update()
    lots = list(lots_query.only(
        "id", "part_type_id", "batch_line_id", "quantity",
        "receipt_customer_price_snapshot_rub",
    ))
    candidate_lots = [
        lot for lot in lots if lot.receipt_customer_price_snapshot_rub is None
    ]
    batch_line_ids = {lot.batch_line_id for lot in candidate_lots}

    receipt_lines_by_batch_line: dict[int, list[ReceiptLine]] = defaultdict(list)
    for line in ReceiptLine.objects.filter(batch_line_id__in=batch_line_ids).only(
        "id", "receipt_id", "part_type_id", "batch_line_id"
    ):
        receipt_lines_by_batch_line[line.batch_line_id].append(line)

    receipt_ids = {
        line.receipt_id
        for lines in receipt_lines_by_batch_line.values()
        for line in lines
    }
    sessions_by_receipt: dict[int, list[InventoryCountingSession]] = defaultdict(list)
    for session in InventoryCountingSession.objects.filter(
        converted_receipt_id__in=receipt_ids,
        status=InventoryCountingSession.Status.POSTED,
    ).only("id", "converted_receipt_id"):
        sessions_by_receipt[session.converted_receipt_id].append(session)

    session_ids = {
        session.id for sessions in sessions_by_receipt.values() for session in sessions
    }
    part_ids = {lot.part_type_id for lot in candidate_lots}
    lines_by_session_part: dict[tuple[int, int], list[InventoryCountingLine]] = defaultdict(list)
    for line in InventoryCountingLine.objects.filter(
        session_id__in=session_ids, warehouse_part_id__in=part_ids
    ).only("id", "session_id", "warehouse_part_id", "final_customer_price_rub"):
        lines_by_session_part[(line.session_id, line.warehouse_part_id)].append(line)

    rows: list[HistoricalPriceBackfillRow] = []
    for lot in lots:
        common = dict(
            lot_id=lot.id,
            part_type_id=lot.part_type_id,
            quantity=lot.quantity,
            receipt_id=None,
            session_id=None,
            counting_line_id=None,
            evidence_price=None,
            previous_snapshot=lot.receipt_customer_price_snapshot_rub,
        )
        if lot.receipt_customer_price_snapshot_rub is not None:
            rows.append(
                HistoricalPriceBackfillRow(
                    **common, outcome="skipped", reason="snapshot_exists"
                )
            )
            continue

        receipt_lines = receipt_lines_by_batch_line.get(lot.batch_line_id, [])
        if len(receipt_lines) != 1:
            reason = "no_receipt_link" if not receipt_lines else "ambiguous_receipt_link"
            rows.append(HistoricalPriceBackfillRow(**common, outcome="skipped", reason=reason))
            continue
        receipt_line = receipt_lines[0]
        common["receipt_id"] = receipt_line.receipt_id
        if receipt_line.part_type_id != lot.part_type_id:
            rows.append(
                HistoricalPriceBackfillRow(
                    **common, outcome="skipped", reason="part_type_mismatch"
                )
            )
            continue

        sessions = sessions_by_receipt.get(receipt_line.receipt_id, [])
        if len(sessions) != 1:
            reason = "no_posted_counting_session" if not sessions else "ambiguous_counting_session"
            rows.append(HistoricalPriceBackfillRow(**common, outcome="skipped", reason=reason))
            continue
        session = sessions[0]
        common["session_id"] = session.id
        evidence = lines_by_session_part.get((session.id, lot.part_type_id), [])
        if len(evidence) != 1:
            values = {line.final_customer_price_rub for line in evidence}
            if not evidence:
                reason = "no_exact_part_evidence"
            elif len(values) > 1:
                reason = "conflicting_evidence"
            else:
                reason = "ambiguous_evidence"
            rows.append(HistoricalPriceBackfillRow(**common, outcome="skipped", reason=reason))
            continue
        evidence_line = evidence[0]
        value = evidence_line.final_customer_price_rub
        common["counting_line_id"] = evidence_line.id
        common["evidence_price"] = value
        if value is None or value <= 0:
            rows.append(
                HistoricalPriceBackfillRow(
                    **common, outcome="skipped", reason="nonpositive_evidence"
                )
            )
            continue
        # The destination is a two-decimal immutable money field.  Never
        # silently round forensic evidence into a different historical price.
        if value != value.quantize(Decimal("0.01")):
            rows.append(
                HistoricalPriceBackfillRow(
                    **common, outcome="skipped", reason="unsupported_precision"
                )
            )
            continue
        rows.append(
            HistoricalPriceBackfillRow(
                **common, outcome="eligible", reason="unique_exact_evidence"
            )
        )
    return HistoricalPriceBackfillPlan(rows=tuple(rows))


def apply_historical_price_backfill() -> HistoricalPriceBackfillPlan:
    """Apply only a freshly locked, deterministic plan in one transaction."""
    with transaction.atomic():
        plan = build_historical_price_backfill_plan(lock_lots=True)
        eligible = plan.eligible
        lots = {
            lot.id: lot
            for lot in StockLot.objects.select_for_update().filter(
                pk__in=[row.lot_id for row in eligible]
            )
        }
        # The plan was made under locks.  These checks turn an unexpected
        # model-level change into a rollback instead of an overwrite.
        for row in eligible:
            lot = lots.get(row.lot_id)
            if (
                lot is None
                or lot.receipt_customer_price_snapshot_rub is not None
                or lot.status != StockLot.Status.AVAILABLE
                or lot.quantity <= 0
                or lot.part_type_id != row.part_type_id
            ):
                raise RuntimeError(f"Backfill target changed while locked: StockLot {row.lot_id}")
        for row in eligible:
            lots[row.lot_id].receipt_customer_price_snapshot_rub = row.evidence_price
        StockLot.objects.bulk_update(
            [lots[row.lot_id] for row in eligible], ["receipt_customer_price_snapshot_rub"]
        )
        return plan
