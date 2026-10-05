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

Two kinds of history are never taken as evidence: a RECEIVE_LOT written by
the old `backfill_opening_movements` (comment "Открывающий остаток"; it gave
transfer targets and pending lots false receipts), and a lot's current batch
line when a receipt says otherwise (before 2c64484 admin could reassign a
lot to another line). A receipt belongs to the line its movement names.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .models import StockLot, StockMovement, StockTransfer

PENDING_RECEIPT = "pending_receipt"
PRIMARY_RECEIPT = "primary_receipt"
LEGACY_PRIMARY = "legacy_primary_receipt"
TRANSFER_DERIVED = "transfer_derived"
RETURN_DERIVED = "return_derived"
RECOUNT_DERIVED = "recount_derived"
FOUND_STOCK = "found_stock"
REASSIGNED = "received_on_another_line"
UNKNOWN = "unknown"

CLASSES = (
    PENDING_RECEIPT, PRIMARY_RECEIPT, LEGACY_PRIMARY, TRANSFER_DERIVED,
    RETURN_DERIVED, RECOUNT_DERIVED, FOUND_STOCK, REASSIGNED, UNKNOWN,
)

# A lot and the movement that opened it are written in one transaction
# (milliseconds); a person cannot create, receive and merge into a lot that fast.
SAME_TRANSACTION = timedelta(seconds=1)
OLD_BACKFILL_COMMENT = "Открывающий остаток"

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


def is_receipt_evidence(movement) -> bool:
    return movement.movement_type == M.RECEIVE_LOT and not (
        movement.comment == OLD_BACKFILL_COMMENT and not movement.document_type
    )


def classify_lot(lot, own, line_movements, transfers) -> LotProvenance:
    """Classify one lot on its current line.

    `own`: every movement of the lot, whatever line it names, by time.
    `line_movements`: every movement naming the lot's line, by time.
    `transfers`: StockTransfer rows by id.
    """
    def result(provenance, intake, evidence):
        return LotProvenance(lot.pk, lot.batch_line_id, lot.status, provenance, intake, evidence)

    if lot.status == StockLot.Status.RECEIVING:
        return result(PENDING_RECEIPT, lot.quantity, "лот на приёмке")
    receipts = [m for m in own if is_receipt_evidence(m)]
    if receipts:
        here = [m for m in receipts if m.batch_line_id == lot.batch_line_id]
        if not here:
            lines = sorted({m.batch_line_id for m in receipts})
            return result(REASSIGNED, Decimal("0"), f"принят по строке {lines}")
        return result(
            PRIMARY_RECEIPT, sum((m.quantity for m in here), Decimal("0")),
            f"RECEIVE_LOT x{len(here)}",
        )
    timeline = _location_timeline(lot, own)
    for m in line_movements:
        transfer = transfers.get(m.document_id)
        if (
            m.stock_lot_id != lot.pk
            and m.movement_type == M.MOVE_LOT
            and m.document_type == TRANSFER_DOC
            and transfer is not None
            and transfer.part_type_id == lot.part_type_id
            and transfer.to_location_id == timeline[0][1]
            and m.to_location_id == timeline[0][1]
            and m.quantity == lot.initial_quantity
            # Created inside the transfer: after its StockTransfer row, before
            # the movement. A lot that existed before the transfer was merged into.
            and transfer.created_at <= lot.created_at <= m.created_at
            and _same_transaction(m.created_at, lot.created_at)
        ):
            return result(
                TRANSFER_DERIVED, Decimal("0"),
                f"перемещение #{transfer.pk}: движение #{m.pk} исходного лота #{m.stock_lot_id}",
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
            # Opened by a found-stock posting on the synthetic batch line made
            # for it: that posting is the whole intake of its own line.
            return result(
                FOUND_STOCK, lot.batch_line.quantity,
                f"найденные детали: строка {lot.batch_line_id}",
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


@dataclass(frozen=True)
class LineProvenance:
    lots: list
    # Receipts naming this line whose lot now belongs to another line.
    receipts_elsewhere: Decimal


def _read_line(line, exclude_lot):
    lots = list(
        StockLot.objects.filter(batch_line=line)
        .exclude(pk=getattr(exclude_lot, "pk", None))
        .select_related("batch_line")
    )
    lot_ids = [lot.pk for lot in lots]
    movements = list(
        StockMovement.objects.filter(batch_line=line).order_by("created_at", "pk")
    )
    foreign = list(
        StockMovement.objects.filter(stock_lot_id__in=lot_ids)
        .exclude(batch_line=line)
        .order_by("created_at", "pk")
    )
    return lots, movements, foreign


def line_provenance_detail(line, *, exclude_lot=None, attempts=3) -> LineProvenance:
    """Classify every lot of a line from one consistent picture of the ledger.

    Lots and movements are separate reads. Every quantity change of a lot
    writes a movement in the same transaction, so if the set of the line's
    movements is the same before and after the lots are read, no change
    committed in between; otherwise the reads are repeated.
    """
    for _attempt in range(attempts):
        before = set(StockMovement.objects.filter(batch_line=line).values_list("pk", flat=True))
        lots, movements, foreign = _read_line(line, exclude_lot)
        if {m.pk for m in movements} == before:
            break
    own: dict[int, list] = {}
    for m in sorted([*movements, *foreign], key=lambda m: (m.created_at, m.pk)):
        if m.stock_lot_id:
            own.setdefault(m.stock_lot_id, []).append(m)
    transfers = StockTransfer.objects.in_bulk(
        {m.document_id for m in movements if m.document_type == TRANSFER_DOC and m.document_id}
    )
    lot_ids = {lot.pk for lot in lots}
    elsewhere = sum(
        (
            m.quantity for m in movements
            if is_receipt_evidence(m) and m.stock_lot_id not in lot_ids
            and (exclude_lot is None or m.stock_lot_id != exclude_lot.pk)
        ),
        Decimal("0"),
    )
    rows = [classify_lot(lot, own.get(lot.pk, []), movements, transfers) for lot in lots]
    return LineProvenance(rows, elsewhere)


def line_provenance(line, *, exclude_lot=None) -> list[LotProvenance]:
    return line_provenance_detail(line, exclude_lot=exclude_lot).lots
