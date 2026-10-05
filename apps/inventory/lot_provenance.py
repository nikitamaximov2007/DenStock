"""Where a stock lot's quantity came from, proven from the movement ledger.

Receiving capacity of a batch line (AUD-01) needs to know how much of the line
was ever received. A lot proves that with its RECEIVE_LOT movement, but
production also holds lots without one, for reasons the code history explains:

* a transfer opens its target lot without a movement of its own - the
  MOVE_LOT is recorded on the SOURCE lot (`_move_locked_lot_portion`);
* before 108b5ad (2026-09-29) the lot status button flipped RECEIVING to
  AVAILABLE/QUARANTINE without `receive_stock_lot`, so a real receipt left
  no RECEIVE_LOT;
* returns, section recounts and found stock open lots that are filled by
  RETURN_LOT / ADJUST_IN, never by a receipt.

Each lot is classified only from evidence; nothing is inferred from a lot
merely having no movement, and nothing is written. UNKNOWN stays unknown.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .models import StockLot, StockMovement

PENDING_RECEIPT = "pending_receipt"
PRIMARY_RECEIPT = "primary_receipt"
LEGACY_PRIMARY = "legacy_primary_receipt"
TRANSFER_DERIVED = "transfer_derived"
RETURN_DERIVED = "return_derived"
RECOUNT_DERIVED = "recount_derived"
FOUND_STOCK = "found_stock"
UNKNOWN = "unknown"

CLASSES = (
    PENDING_RECEIPT, PRIMARY_RECEIPT, LEGACY_PRIMARY, TRANSFER_DERIVED,
    RETURN_DERIVED, RECOUNT_DERIVED, FOUND_STOCK, UNKNOWN,
)

# A lot and the movement that opened it are written in one transaction.
SAME_TRANSACTION = timedelta(seconds=5)

M = StockMovement.MovementType
INFLOW = {M.ADJUST_IN, M.RETURN_LOT, M.WRITE_OFF_CANCEL_LOT}
OUTFLOW = {M.ADJUST_OUT, M.SALE_LOT, M.ISSUE_LOT, M.WRITE_OFF_LOT}
TRANSFER_DOC = "stock_transfer"


@dataclass(frozen=True)
class LotProvenance:
    lot_id: int
    batch_line_id: int
    status: str
    provenance: str
    intake: Decimal | None  # quantity received from the line; None when unproven
    evidence: str


def _same_transaction(a, b) -> bool:
    return abs(a - b) <= SAME_TRANSACTION


def _location_timeline(lot, own):
    """(time, location) pairs: where the lot stood, changed only by whole-lot moves."""
    whole_moves = [m for m in own if m.movement_type == M.MOVE_LOT and not m.document_type]
    start = whole_moves[0].from_location_id if whole_moves else lot.location_id
    timeline = [(lot.created_at, start)]
    timeline += [(m.created_at, m.to_location_id) for m in whole_moves]
    return timeline


def _location_at(timeline, moment):
    location = timeline[0][1]
    for when, where in timeline:
        if when <= moment:
            location = where
    return location


def _reconstructed_start(lot, own, line_movements, timeline) -> Decimal:
    """The lot's quantity before any movement, rebuilt backwards from today.

    Own inflows and outflows, transfer portions taken out of this lot and
    transfer portions merged into it (recorded on their source lots) are all
    in the ledger; a whole-lot move changes no quantity.
    """
    net = Decimal("0")
    for m in own:
        if m.movement_type in INFLOW:
            net += m.quantity
        elif m.movement_type in OUTFLOW:
            net -= m.quantity
        elif m.movement_type == M.MOVE_LOT and m.document_type == TRANSFER_DOC:
            net -= m.quantity
    for m in line_movements:
        if (
            m.stock_lot_id != lot.pk
            and m.movement_type == M.MOVE_LOT
            and m.document_type == TRANSFER_DOC
            and m.created_at > lot.created_at
            and m.to_location_id == _location_at(timeline, m.created_at)
        ):
            net += m.quantity
    return lot.quantity - net


def classify_lot(lot, own, line_movements) -> LotProvenance:
    """Classify one lot; `own` and `line_movements` are ordered by time."""
    def result(provenance, intake, evidence):
        return LotProvenance(lot.pk, lot.batch_line_id, lot.status, provenance, intake, evidence)

    if lot.status == StockLot.Status.RECEIVING:
        return result(PENDING_RECEIPT, lot.quantity, "лот на приёмке")
    receipts = [m for m in own if m.movement_type == M.RECEIVE_LOT]
    if receipts:
        return result(
            PRIMARY_RECEIPT, sum((m.quantity for m in receipts), Decimal("0")),
            f"RECEIVE_LOT x{len(receipts)}",
        )
    timeline = _location_timeline(lot, own)
    for m in line_movements:
        if (
            m.stock_lot_id != lot.pk
            and m.movement_type == M.MOVE_LOT
            and m.document_type == TRANSFER_DOC
            and m.to_location_id == timeline[0][1]
            and m.quantity == lot.initial_quantity
            and _same_transaction(m.created_at, lot.created_at)
        ):
            return result(
                TRANSFER_DERIVED, Decimal("0"),
                f"перемещение #{m.document_id}: движение #{m.pk} исходного лота #{m.stock_lot_id}",
            )
    first = own[0] if own else None
    if (
        first is not None
        and first.movement_type == M.RETURN_LOT
        and first.quantity == lot.initial_quantity
        and _same_transaction(first.created_at, lot.created_at)
    ):
        return result(RETURN_DERIVED, Decimal("0"), f"возврат: движение #{first.pk}")
    if lot.initial_quantity == 0 and first is not None and first.movement_type == M.ADJUST_IN:
        if first.document_type == "section_recount":
            return result(RECOUNT_DERIVED, Decimal("0"), f"пересчёт #{first.document_id}")
        if first.document_type == "found_addition":
            found = [
                m for m in own
                if m.movement_type == M.ADJUST_IN and m.document_type == "found_addition"
            ]
            return result(
                FOUND_STOCK, sum((m.quantity for m in found), Decimal("0")),
                f"найденные детали x{len(found)}",
            )
    if lot.initial_quantity > 0:
        start = _reconstructed_start(lot, own, line_movements, timeline)
        if start == lot.initial_quantity:
            return result(
                LEGACY_PRIMARY, lot.initial_quantity,
                f"приёмка без движения; журнал восстанавливает {start} = исходному",
            )
        return result(
            UNKNOWN, None,
            f"журнал восстанавливает {start}, исходное {lot.initial_quantity}",
        )
    return result(UNKNOWN, None, "нет доказательства происхождения")


def line_provenance(line, *, exclude_lot=None) -> list[LotProvenance]:
    lots = list(
        StockLot.objects.filter(batch_line=line).exclude(pk=getattr(exclude_lot, "pk", None))
    )
    movements = list(
        StockMovement.objects.filter(batch_line=line).order_by("created_at", "pk")
    )
    own: dict[int, list] = {}
    for m in movements:
        if m.stock_lot_id:
            own.setdefault(m.stock_lot_id, []).append(m)
    return [classify_lot(lot, own.get(lot.pk, []), movements) for lot in lots]
