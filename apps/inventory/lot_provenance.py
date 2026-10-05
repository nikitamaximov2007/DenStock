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

from django.db.models import Count, Q

from apps.procurement.models import BatchLine

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


def _transfer_candidates(lot, own, nearby_moves, nearby_transfers, transfers):
    """Find secondary-origin evidence around lot creation, not just healthy moves."""
    original_location = _location_timeline(lot, own)[0][1]
    possible = []
    represented_transfers = set()
    for movement in nearby_moves:
        if movement.stock_lot_id == lot.pk or not _same_transaction(
            movement.created_at, lot.created_at
        ):
            continue
        transfer = transfers.get(movement.document_id) if movement.document_id else None
        if movement.created_at < lot.created_at:
            continue
        if transfer is not None and transfer.created_at > lot.created_at:
            continue
        if movement.document_type == TRANSFER_DOC or (
            movement.movement_type == M.MOVE_LOT
            and movement.to_location_id == original_location
        ):
            possible.append((movement, transfer, original_location))
            if transfer is not None:
                represented_transfers.add(transfer.pk)
    for transfer in nearby_transfers:
        if (
            transfer.pk not in represented_transfers
            and transfer.to_location_id == original_location
            and transfer.part_type_id == lot.part_type_id
            and transfer.created_at <= lot.created_at
            and _same_transaction(transfer.created_at, lot.created_at)
        ):
            possible.append((None, transfer, original_location))
    return possible


def _transfer_is_consistent(lot, candidate, transfer_movements, unique_line_keys) -> bool:
    movement, transfer, original_location = candidate
    if movement is None or transfer is None:
        return False
    if not (
        movement.movement_type == M.MOVE_LOT
        and movement.document_type == TRANSFER_DOC
        and transfer.part_item_id is None
        and transfer.stock_state in (StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE)
        and movement.quantity == lot.initial_quantity
        and movement.to_location_id == original_location
        and transfer.to_location_id == original_location
        and movement.from_location_id == transfer.from_location_id
        and transfer.created_at <= lot.created_at <= movement.created_at
        and _same_transaction(transfer.created_at, lot.created_at)
        and _same_transaction(movement.created_at, lot.created_at)
        and movement.part_type_id == transfer.part_type_id == lot.part_type_id
        and movement.batch_id == lot.batch_id
        and (lot.batch_id, lot.part_type_id) in unique_line_keys
        and lot.batch_line.part_type_id == lot.part_type_id
        and movement.batch_line_id == lot.batch_line_id
        and movement.batch_line_id is not None
        and movement.stock_lot.batch_line_id == movement.batch_line_id
        and movement.batch_line.part_type_id == movement.part_type_id
        and movement.batch_id == movement.stock_lot.batch_id
        and movement.batch_line.batch_id == movement.batch_id
        and movement.stock_lot.part_type_id == movement.part_type_id
    ):
        return False

    rows = transfer_movements.get(transfer.pk, [])
    if not rows or sum((row.quantity for row in rows), Decimal("0")) != transfer.quantity:
        return False
    for row in rows:
        source = row.stock_lot
        if not (
            row.movement_type == M.MOVE_LOT
            and row.document_type == TRANSFER_DOC
            and row.part_type_id == transfer.part_type_id
            and row.from_location_id == transfer.from_location_id
            and row.to_location_id == transfer.to_location_id
            and source is not None
            and source.part_type_id == transfer.part_type_id
            and source.batch_line_id == row.batch_line_id
            and row.batch_id == source.batch_id
            and row.batch_line_id is not None
            and row.batch_line.part_type_id == transfer.part_type_id
            and row.batch_line.batch_id == row.batch_id
        ):
            return False
    return True


def is_receipt_evidence(movement) -> bool:
    return movement.movement_type == M.RECEIVE_LOT and not (
        movement.comment == OLD_BACKFILL_COMMENT and not movement.document_type
    )


def classify_lot(
    lot, own, line_movements, transfers, nearby_moves, nearby_transfers,
    transfer_movements, legacy_line_keys,
) -> LotProvenance:
    """Classify one lot on its current line.

    `own`: every movement of the lot, whatever line it names, by time.
    `line_movements`: every movement naming the lot's line, by time.
    `transfers`: StockTransfer rows by id.
    `nearby_moves`: transfer-adjacent movements, including rows on another line.
    `transfer_movements`: complete MOVE_LOT groups keyed by StockTransfer id.
    """
    def result(provenance, intake, evidence):
        return LotProvenance(lot.pk, lot.batch_line_id, lot.status, provenance, intake, evidence)

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
    candidates = _transfer_candidates(
        lot, own, nearby_moves, nearby_transfers, transfers
    )
    if candidates:
        identified = [
            candidate
            for candidate in candidates
            if candidate[0] is not None
            and candidate[0].quantity == lot.initial_quantity
            and candidate[0].batch_id == lot.batch_id
        ]
        if identified:
            transfer_ids = {
                candidate[1].pk for candidate in identified if candidate[1] is not None
            }
            if any(
                candidate[1] is None or candidate[1].pk not in transfer_ids
                for candidate in candidates
            ):
                return result(UNKNOWN, None, "неоднозначная цепочка перемещения")
            candidates = identified
        valid = [
            candidate
            for candidate in candidates
            if _transfer_is_consistent(
                lot, candidate, transfer_movements, legacy_line_keys
            )
        ]
        if len(candidates) == 1 and len(valid) == 1:
            m, transfer, _location = valid[0]
            return result(
                TRANSFER_DERIVED, Decimal("0"),
                f"перемещение #{transfer.pk}: движение #{m.pk} исходного лота #{m.stock_lot_id}",
            )
        return result(UNKNOWN, None, "неоднозначная или повреждённая цепочка перемещения")
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
    old_backfill = any(
        movement.movement_type == M.RECEIVE_LOT
        and movement.comment == OLD_BACKFILL_COMMENT
        and not movement.document_type
        for movement in own
    )
    if lot.status == StockLot.Status.RECEIVING:
        non_backfill_history = any(
            not (
                movement.movement_type == M.RECEIVE_LOT
                and movement.comment == OLD_BACKFILL_COMMENT
                and not movement.document_type
            )
            for movement in own
        )
        if non_backfill_history:
            return result(UNKNOWN, None, "статус приёмки противоречит журналу движений")
        return result(PENDING_RECEIPT, lot.quantity, "лот на приёмке без истории приёмки")
    if old_backfill:
        return result(UNKNOWN, None, "открывающая запись журнала не доказывает приёмку")
    if lot.initial_quantity > 0:
        timeline = _location_timeline(lot, own)
        if (
            lot.batch_id != lot.batch_line.batch_id
            or lot.batch_line.part_type_id != lot.part_type_id
            or (lot.batch_id, lot.part_type_id) not in legacy_line_keys
        ):
            return result(UNKNOWN, None, "текущая строка партии не доказывает происхождение лота")
        start = _reconstructed_start(lot, own, line_movements, timeline)
        if start == lot.initial_quantity:
            historical_lines = {
                movement.batch_line_id
                for movement in own
                if movement.stock_lot_id == lot.pk and movement.batch_line_id is not None
            }
            if historical_lines - {lot.batch_line_id}:
                return result(
                    UNKNOWN,
                    None,
                    "история лота ссылается на другую строку партии",
                )
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
    # Legacy movements still name a line after their lot was rebound elsewhere.
    # Without a RECEIVE_LOT, the source line's lifetime intake is unprovable.
    has_unproven_detached_history: bool = False


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


def _group_own_movements(movements, foreign):
    own: dict[int, list] = {}
    for movement in sorted([*movements, *foreign], key=lambda m: (m.created_at, m.pk)):
        if movement.stock_lot_id:
            own.setdefault(movement.stock_lot_id, []).append(movement)
    return own


def _nearby_origin_movements(lots):
    creation_windows = Q(pk__in=[])
    for lot in lots:
        creation_windows |= Q(
            created_at__gte=lot.created_at - SAME_TRANSACTION,
            created_at__lte=lot.created_at + SAME_TRANSACTION,
        )
    return list(
        StockMovement.objects.filter(creation_windows)
        .select_related("stock_lot", "batch_line")
        .order_by("created_at", "pk")
    )


def _nearby_transfer_documents(lots, own):
    transfer_windows = Q(pk__in=[])
    for lot in lots:
        original_location = _location_timeline(lot, own.get(lot.pk, []))[0][1]
        transfer_windows |= Q(
            created_at__gte=lot.created_at - SAME_TRANSACTION,
            created_at__lte=lot.created_at,
            to_location_id=original_location,
        )
    return list(StockTransfer.objects.filter(transfer_windows).order_by("created_at", "pk"))


def line_provenance_detail(line, *, exclude_lot=None, attempts=3) -> LineProvenance:
    """Classify every lot of a line from one consistent picture of the ledger.

    Lots and movements are separate reads. Every quantity change of a lot
    writes a movement in the same transaction. Transfer evidence can name a
    different line, so both the line ledger and movements around each
    candidate lot's creation are checked before and after the reads; otherwise
    the picture is retried.
    """
    for _attempt in range(attempts):
        lot_query = StockLot.objects.filter(batch_line=line).exclude(
            pk=getattr(exclude_lot, "pk", None)
        )
        lots_before = set(lot_query.values_list("pk", flat=True))
        before = set(StockMovement.objects.filter(batch_line=line).values_list("pk", flat=True))
        lots, movements, foreign = _read_line(line, exclude_lot)
        own = _group_own_movements(movements, foreign)
        nearby_moves = _nearby_origin_movements(lots)
        nearby_transfers = _nearby_transfer_documents(lots, own)
        lots_after = set(lot_query.values_list("pk", flat=True))
        after = set(StockMovement.objects.filter(batch_line=line).values_list("pk", flat=True))
        nearby_after = _nearby_origin_movements(lots)
        transfers_after = _nearby_transfer_documents(lots, own)
        if (
            {lot.pk for lot in lots} == lots_before == lots_after
            and {m.pk for m in movements} == before == after
            and {m.pk for m in nearby_moves} == {m.pk for m in nearby_after}
            and {t.pk for t in nearby_transfers} == {t.pk for t in transfers_after}
        ):
            break
    own = _group_own_movements(movements, foreign)
    transfers = StockTransfer.objects.in_bulk(
        {m.document_id for m in movements if m.document_type == TRANSFER_DOC and m.document_id}
    )
    # Document IDs are only unique within their document type. Loading every
    # integer ID as a StockTransfer can make an unrelated sale/receipt appear
    # to be transfer evidence when the numeric IDs happen to collide.
    transfer_ids = {
        m.document_id
        for m in nearby_moves
        if m.document_id and m.document_type == TRANSFER_DOC
    }
    transfer_ids.update(transfer.pk for transfer in nearby_transfers)
    transfers.update(StockTransfer.objects.in_bulk(transfer_ids))
    transfer_rows = list(
        StockMovement.objects.filter(
            document_type=TRANSFER_DOC,
            document_id__in=transfer_ids,
        ).select_related("stock_lot", "batch_line").order_by("created_at", "pk")
    )
    transfer_movements: dict[int, list] = {}
    for movement in transfer_rows:
        transfer_movements.setdefault(movement.document_id, []).append(movement)
    line_counts = BatchLine.objects.filter(
        batch_id__in={lot.batch_id for lot in lots},
        part_type_id__in={lot.part_type_id for lot in lots},
    ).values("batch_id", "part_type_id").annotate(n=Count("pk"))
    legacy_line_keys = {
        (row["batch_id"], row["part_type_id"])
        for row in line_counts
        if row["n"] == 1
    }
    lot_ids = {lot.pk for lot in lots}
    elsewhere = sum(
        (
            m.quantity for m in movements
            if is_receipt_evidence(m) and m.stock_lot_id not in lot_ids
            and (exclude_lot is None or m.stock_lot_id != exclude_lot.pk)
        ),
        Decimal("0"),
    )
    detached_lot_ids = set(
        StockMovement.objects.filter(batch_line=line, stock_lot__isnull=False)
        .exclude(stock_lot__batch_line=line)
        .values_list("stock_lot_id", flat=True)
    )
    proven_detached_receipts = set(
        StockMovement.objects.filter(
            stock_lot_id__in=detached_lot_ids,
            batch_line=line,
            movement_type=M.RECEIVE_LOT,
        )
        .exclude(comment=OLD_BACKFILL_COMMENT, document_type="")
        .values_list("stock_lot_id", flat=True)
    )
    unproven_detached_history = bool(detached_lot_ids - proven_detached_receipts)
    rows = [
        classify_lot(
            lot, own.get(lot.pk, []), movements, transfers, nearby_moves,
            nearby_transfers,
            transfer_movements, legacy_line_keys,
        )
        for lot in lots
    ]
    return LineProvenance(rows, elsewhere, unproven_detached_history)


def line_provenance(line, *, exclude_lot=None) -> list[LotProvenance]:
    return line_provenance_detail(line, exclude_lot=exclude_lot).lots
