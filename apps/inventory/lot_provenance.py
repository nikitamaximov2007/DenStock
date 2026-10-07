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
import re
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


def _transfer_note_id(lot):
    """Return the persisted transfer-document reference carried by a new target lot."""
    match = re.match(r"^Перемещение #(\d+) из ", lot.note or "")
    return int(match.group(1)) if match else None


def _has_transfer_note(lot) -> bool:
    return (lot.note or "").startswith("Перемещение #")


def _location_timeline(lot, own):
    """(time, location) pairs: where the lot stood, changed only by whole-lot moves."""
    whole_moves = [
        m for m in own
        if m.movement_type == M.MOVE_LOT
        and not m.document_type
        and m.from_location_id is not None
        and m.to_location_id is not None
    ]
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
    """Find transfer evidence tied to this exact lot identity near its creation."""
    if lot.status == StockLot.Status.RECEIVING:
        return []
    original_location = _location_timeline(lot, own)[0][1]
    possible = []
    represented_transfers = set()
    for movement in nearby_moves:
        if movement.stock_lot_id == lot.pk:
            continue
        transfer = transfers.get(movement.document_id) if movement.document_id else None
        exact_identity = (
            movement.movement_type == M.MOVE_LOT
            and movement.document_type == TRANSFER_DOC
            and movement.batch_id == lot.batch_id
            and movement.batch_line_id == lot.batch_line_id
            and movement.part_type_id == lot.part_type_id
            and movement.to_location_id == original_location
            and movement.quantity == lot.initial_quantity
            and transfer is not None
            and transfer.part_type_id == lot.part_type_id
            and transfer.to_location_id == original_location
        )
        if exact_identity and _same_transaction(movement.created_at, lot.created_at):
            possible.append((movement, transfer, original_location))
            represented_transfers.add(transfer.pk)
    return possible


def _transfer_is_consistent(
    lot, transfer, rows, original_location, conflicting_source_lots=frozenset()
) -> bool:
    """Validate transfer lineage by document/line/cell relationships, not clocks."""
    if transfer is None or not (
        transfer.part_item_id is None
        and transfer.stock_state in (StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE)
        and transfer.part_type_id == lot.part_type_id
        and transfer.to_location_id == original_location
        and lot.batch_line.part_type_id == lot.part_type_id
        and rows
    ):
        return False
    if not rows or sum((row.quantity for row in rows), Decimal("0")) != transfer.quantity:
        return False
    target_rows = [
        row for row in rows
        if row.batch_line_id == lot.batch_line_id
        and row.to_location_id == original_location
    ]
    if sum((row.quantity for row in target_rows), Decimal("0")) != lot.initial_quantity:
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
            and source.pk not in conflicting_source_lots
        ):
            return False
    return True


def _return_document_consistent(rows, movements, return_id):
    """Every posted row of this document must have one matching journal row."""
    lines = [line for line in rows if line.stock_return_id == return_id]
    if not lines or len(lines) != len(movements):
        return False
    matched = set()
    for movement in movements:
        if movement.movement_type == M.RETURN_LOT:
            candidates = [
                line for line in lines
                if line.returned_lot_id == movement.stock_lot_id
                and movement.stock_lot_id is not None
                and line.part_item_id is None
            ]
        elif movement.movement_type == M.RETURN_ITEM:
            candidates = [
                line for line in lines
                if line.part_item_id == movement.part_item_id
                and movement.part_item_id is not None
            ]
        else:
            return False
        candidates = [
            line for line in candidates
            if line.batch_id == movement.batch_id
            and line.batch_line_id == movement.batch_line_id
            and line.part_type_id == movement.part_type_id
            and line.to_location_id == movement.to_location_id
            and line.quantity == movement.quantity
        ]
        if len(candidates) != 1 or candidates[0].pk in matched:
            return False
        matched.add(candidates[0].pk)
    return len(matched) == len(lines)


def _return_origin_matches(lot, row, own, rows, doc_movements, *, explicit):
    """Validate a return as the lot's creation event, not merely later inflow."""
    original_location = _location_timeline(lot, own)[0][1]
    ret = row.stock_return
    if not (
        row.returned_lot_id == lot.pk
        and ret.status in (ret.Status.COMPLETED, ret.Status.CANCELED)
        and ret.completed_at is not None
        and row.batch_id == lot.batch_id
        and row.batch_line_id == lot.batch_line_id
        and row.part_type_id == lot.part_type_id
        and row.to_location_id == original_location
        and row.quantity == lot.initial_quantity
    ):
        return False
    linked = [
        movement for movement in own
        if movement.document_type == "stock_return"
        and movement.document_id == ret.pk
    ]
    matching = [
        movement for movement in linked
        if (
            movement.movement_type == M.RETURN_LOT
            and movement.batch_id == row.batch_id
            and movement.batch_line_id == row.batch_line_id
            and movement.part_type_id == row.part_type_id
            and movement.to_location_id == row.to_location_id
            and movement.quantity == row.quantity
        )
    ]
    same_lot_lines = [
        candidate for candidate in rows
        if candidate.stock_return_id == ret.pk and candidate.returned_lot_id == lot.pk
    ]
    if (
        len(linked) != 1 or len(same_lot_lines) != 1
        or not _return_document_consistent(rows, doc_movements.get(ret.pk, []), ret.pk)
    ):
        return False
    if explicit:
        # The creation marker is independent of this FK. The first ledger row
        # must also be the exact return, never a later replenishment of the lot.
        return bool(len(matching) == 1 and own and own[0].pk == matching[0].pk)
    return bool(
        len(matching) == 1
        and own
        and own[0].pk == matching[0].pk
        and _same_transaction(matching[0].created_at, lot.created_at)
        and _same_transaction(ret.completed_at, lot.created_at)
    )


def _return_origin_state(lot, rows, own, doc_movements):
    """Return True for proven creation, False for no origin evidence, None if damaged."""
    explicit_id = getattr(lot, "origin_return_line_id", None)
    if explicit_id:
        row = next((item for item in rows if item.pk == explicit_id), None)
        return bool(row and _return_origin_matches(
            lot, row, own, rows, doc_movements, explicit=True
        ))
    # A return line is created with the draft, often minutes or days before the
    # physical stock is posted. Its created_at therefore says nothing about lot
    # origin. Use the document's completion time and the first lot movement as
    # event evidence instead.
    first = own[0] if own else None
    first_is_creation_time = bool(
        first and _same_transaction(first.created_at, lot.created_at)
    )
    # A found/recount posting is itself the first, creation-time stock event.
    # A return posted immediately afterwards into that existing lot cannot
    # retroactively replace this stronger origin evidence.
    if (
        first_is_creation_time and first.movement_type == M.ADJUST_IN
        and first.document_type in {"found_addition", "section_recount"}
    ):
        return False
    first_return_movement = bool(
        first_is_creation_time
        and first.movement_type == M.RETURN_LOT
        and first.document_type == "stock_return"
    )
    associated_rows = [
        row for row in rows
        if row.returned_lot_id == lot.pk
        or (
            first_return_movement
            and first.document_id is not None
            and row.stock_return_id == first.document_id
        )
    ]
    completion_rows = [
        row for row in associated_rows
        if row.stock_return.completed_at is not None
        and _same_transaction(row.stock_return.completed_at, lot.created_at)
    ]
    has_creation_evidence = first_is_creation_time and (
        first.movement_type == M.RETURN_LOT
        or first.document_type == "stock_return"
    )
    if not has_creation_evidence and not completion_rows:
        # A return into a pre-existing lot is later stock flow, even if it is
        # the first movement still present on that lot.
        return False

    candidate_rows = {row.pk: row for row in [*associated_rows, *completion_rows]}
    candidates = [
        row for row in candidate_rows.values()
        if _return_origin_matches(lot, row, own, rows, doc_movements, explicit=False)
    ]
    if len(candidates) == 1:
        return True
    if candidates or candidate_rows:
        # A surviving document/line that contradicts the first stock event is
        # evidence of damage, not permission to fall back to supplier intake.
        return None

    # The movement alone cannot prove that its return document completed or
    # that the exact return line targeted this lot. Missing document/line
    # evidence therefore fails closed instead of reconstructing a relationship.
    return None


def is_receipt_evidence(movement) -> bool:
    return movement.movement_type == M.RECEIVE_LOT and not (
        movement.comment == OLD_BACKFILL_COMMENT and not movement.document_type
    )


def classify_lot(
    lot, own, line_movements, transfers, nearby_moves, nearby_transfers,
    transfer_movements, legacy_line_keys, return_rows=(),
    conflicting_source_lots=frozenset(), unanchored_transfer_lots=frozenset(),
    return_doc_movements=None,
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

    marker = lot.creation_origin
    if lot.origin_transfer_id and lot.origin_return_line_id:
        return result(UNKNOWN, None, "два несовместимых явных источника лота")
    if lot.origin_transfer_id and marker != StockLot.CreationOrigin.TRANSFER:
        return result(UNKNOWN, None, "перемещение не является путём создания лота")
    if lot.origin_return_line_id and marker != StockLot.CreationOrigin.RETURN:
        return result(UNKNOWN, None, "возврат не является путём создания лота")
    if marker == StockLot.CreationOrigin.TRANSFER and not lot.origin_transfer_id:
        return result(UNKNOWN, None, "ссылка на создавшее перемещение отсутствует")
    if marker == StockLot.CreationOrigin.RETURN and not lot.origin_return_line_id:
        return result(UNKNOWN, None, "ссылка на создавший возврат отсутствует")

    # Validate claimed derived origins before considering any supplier receipt.
    if lot.origin_transfer_id or lot.origin_return_line_id:
        if lot.origin_transfer_id:
            transfer_id = lot.origin_transfer_id
            note_transfer_id = _transfer_note_id(lot)
            transfer = transfers.get(transfer_id)
            rows = transfer_movements.get(transfer_id, [])
            original_location = _location_timeline(lot, own)[0][1]
            if (
                (note_transfer_id is None or note_transfer_id == transfer_id)
                and not any(is_receipt_evidence(m) for m in own)
                and _transfer_is_consistent(
                    lot, transfer, rows, original_location, conflicting_source_lots
                )
            ):
                return result(TRANSFER_DERIVED, Decimal("0"), f"перемещение #{transfer_id}")
            return result(UNKNOWN, None, "связь лота с перемещением повреждена")
        if not any(is_receipt_evidence(m) for m in own):
            if _return_origin_state(
                lot, return_rows, own, return_doc_movements or {}
            ) is True:
                return result(RETURN_DERIVED, Decimal("0"), "строка возврата создала лот")
        return result(UNKNOWN, None, "связь лота с возвратом повреждена")

    return_origin = _return_origin_state(lot, return_rows, own, return_doc_movements or {})
    receipts = [m for m in own if is_receipt_evidence(m)]
    if (
        marker != StockLot.CreationOrigin.SUPPLIER_RECEIVED
        and (return_origin is None or (return_origin is True and receipts))
    ):
        return result(UNKNOWN, None, "свидетельство возврата противоречит приёмке")
    if return_origin is True:
        return result(RETURN_DERIVED, Decimal("0"), "строка возврата создала лот")
    if receipts:
        first = next((m for m in own if not (
            m.movement_type == M.RECEIVE_LOT
            and m.comment == OLD_BACKFILL_COMMENT and not m.document_type
        )), None)
        valid_receipt = (
            marker in (None, StockLot.CreationOrigin.SUPPLIER_RECEIVED)
            and not _has_transfer_note(lot)
            and lot.pk not in unanchored_transfer_lots
            and len(receipts) == 1
            and first is not None and first.pk == receipts[0].pk
            and receipts[0].document_type == "" and receipts[0].document_id is None
            and receipts[0].batch_id == lot.batch_id
            and receipts[0].part_type_id == lot.part_type_id
            and receipts[0].batch_line_id is not None
            and receipts[0].batch_line.batch_id == receipts[0].batch_id
            and receipts[0].batch_line.part_type_id == receipts[0].part_type_id
            and receipts[0].to_location_id == _location_timeline(lot, own)[0][1]
            and receipts[0].quantity == lot.initial_quantity
            and lot.initial_quantity > 0
        )
        if not valid_receipt:
            return result(UNKNOWN, None, "движение приёмки не доказывает источник лота")
        here = [m for m in receipts if m.batch_line_id == lot.batch_line_id]
        if not here:
            lines = sorted({m.batch_line_id for m in receipts})
            return result(REASSIGNED, Decimal("0"), f"принят по строке {lines}")
        return result(
            PRIMARY_RECEIPT, sum((m.quantity for m in here), Decimal("0")),
            f"RECEIVE_LOT x{len(here)}",
        )
    if lot.origin_transfer_id or _has_transfer_note(lot):
        transfer_id = lot.origin_transfer_id or _transfer_note_id(lot)
        note_transfer_id = _transfer_note_id(lot)
        transfer = transfers.get(transfer_id) if transfer_id is not None else None
        rows = transfer_movements.get(transfer_id, []) if transfer_id is not None else []
        original_location = _location_timeline(lot, own)[0][1]
        explicitly_linked = lot.origin_transfer_id is not None
        if explicitly_linked and note_transfer_id is not None and note_transfer_id != transfer_id:
            return result(UNKNOWN, None, "ссылка лота противоречит примечанию перемещения")
        # New lots carry a direct FK to their creating transfer. For historical
        # lots, a note is only a hint; clocks remain a narrow compatibility
        # fallback and can never turn contradictory evidence into provenance.
        time_supports_legacy_link = explicitly_linked or (
            transfer is not None
            and transfer.created_at <= lot.created_at
            and _same_transaction(transfer.created_at, lot.created_at)
            and any(
                movement.created_at >= lot.created_at
                and _same_transaction(movement.created_at, lot.created_at)
                for movement in rows
                if movement.batch_line_id == lot.batch_line_id
                and movement.to_location_id == original_location
            )
        )
        if time_supports_legacy_link and _transfer_is_consistent(
            lot, transfer, rows, original_location, conflicting_source_lots
        ):
            return result(TRANSFER_DERIVED, Decimal("0"), f"перемещение #{transfer_id}")
        return result(UNKNOWN, None, "связь лота с документом перемещения повреждена")

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
        candidate_transfer_ids = {
            candidate[1].pk for candidate in candidates if candidate[1] is not None
        }
        valid = [
            transfer_id for transfer_id in candidate_transfer_ids
            if _transfer_is_consistent(
                lot,
                transfers.get(transfer_id),
                transfer_movements.get(transfer_id, []),
                _location_timeline(lot, own)[0][1],
                conflicting_source_lots,
            )
        ]
        if len(candidate_transfer_ids) == 1 and len(valid) == 1:
            transfer = transfers[valid[0]]
            return result(
                TRANSFER_DERIVED, Decimal("0"),
                f"перемещение #{transfer.pk}: документ и движения согласованы",
            )
        return result(UNKNOWN, None, "неоднозначная или повреждённая цепочка перемещения")
    if lot.pk in unanchored_transfer_lots:
        return result(
            UNKNOWN, None,
            "есть перемещение подходящего количества, но его связь с созданием лота не доказана",
        )
    first = own[0] if own else None
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
    if old_backfill:
        return result(UNKNOWN, None, "открывающая запись журнала не доказывает приёмку")
    if lot.initial_quantity > 0:
        if marker != StockLot.CreationOrigin.SUPPLIER_PENDING:
            return result(UNKNOWN, None, "нет подтверждения первоначальной приёмки")
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
        .select_related("batch_line", "origin_transfer", "origin_return_line")
    )
    lot_ids = [lot.pk for lot in lots]
    movements = list(
        StockMovement.objects.filter(batch_line=line)
        .select_related("batch_line").order_by("created_at", "pk")
    )
    foreign = list(
        StockMovement.objects.filter(stock_lot_id__in=lot_ids)
        .exclude(batch_line=line)
        .select_related("batch_line")
        .order_by("created_at", "pk")
    )
    return lots, movements, foreign


def _group_own_movements(movements, foreign):
    own: dict[int, list] = {}
    for movement in sorted([*movements, *foreign], key=lambda m: (m.created_at, m.pk)):
        if movement.stock_lot_id:
            own.setdefault(movement.stock_lot_id, []).append(movement)
    return own


def _lot_signature(lot):
    return (
        lot.pk, lot.batch_id, lot.batch_line_id, lot.part_type_id, lot.location_id,
        lot.quantity, lot.initial_quantity, lot.status, lot.created_at,
        lot.origin_transfer_id, lot.origin_return_line_id, lot.creation_origin,
    )


def _movement_signature(movement):
    return (
        movement.pk, movement.movement_type, movement.stock_lot_id,
        movement.batch_id, movement.batch_line_id, movement.part_type_id,
        movement.quantity, movement.from_location_id, movement.to_location_id,
        movement.document_type, movement.document_id, movement.created_at,
    )


def _movement_snapshot(queryset):
    return list(
        queryset.order_by("pk").values_list(
            "pk", "movement_type", "stock_lot_id", "batch_id", "batch_line_id",
            "part_type_id", "quantity", "from_location_id", "to_location_id",
            "document_type", "document_id", "created_at",
        )
    )


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


def _possible_unanchored_transfer_lots(lots, own):
    """Find transfer evidence that predates a lot with the exact target identity.

    A transfer written before a lot exists cannot be a later inflow to it. Since
    the physical key is (BatchLine, cell), the same exact destination identity
    is strong contradictory evidence even when clocks were shifted by more
    than the normal transaction window. Transfers after lot creation are flows.
    """
    query = Q(pk__in=[])
    lot_by_key = {}
    for lot in lots:
        if lot.status == StockLot.Status.RECEIVING:
            continue
        location_id = _location_timeline(lot, own.get(lot.pk, []))[0][1]
        key = (
            lot.batch_id, lot.batch_line_id, lot.part_type_id, location_id,
            lot.initial_quantity,
        )
        query |= Q(
            batch_id=key[0], batch_line_id=key[1], part_type_id=key[2],
            to_location_id=key[3], movement_type=M.MOVE_LOT,
            document_type=TRANSFER_DOC, quantity=key[4],
        )
        lot_by_key.setdefault(key, []).append(lot)
    matching = list(StockMovement.objects.filter(query).values_list(
        "batch_id", "batch_line_id", "part_type_id", "to_location_id", "quantity", "created_at"
    ))
    return {
        lot.pk
        for key, group in lot_by_key.items()
        for lot in group
        if not lot.origin_transfer_id
        and not _has_transfer_note(lot)
        and any(
            row[:5] == key and row[5] < lot.created_at
            for row in matching
        )
    }


def line_provenance_detail(line, *, exclude_lot=None, attempts=3) -> LineProvenance:
    """Classify every lot of a line from one consistent picture of the ledger.

    Lots and movements are separate reads. Every quantity change of a lot
    writes a movement in the same transaction. Transfer evidence can name a
    different line, so both the line ledger and movements around each
    candidate lot's creation are checked before and after the reads; otherwise
    the picture is retried.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    consistent_snapshot = False
    for _attempt in range(attempts):
        lot_query = StockLot.objects.filter(batch_line=line).exclude(
            pk=getattr(exclude_lot, "pk", None)
        )
        lots_before = list(
            lot_query.order_by("pk").values_list(
                "pk", "batch_id", "batch_line_id", "part_type_id", "location_id",
                "quantity", "initial_quantity", "status", "created_at", "origin_transfer_id",
                "origin_return_line_id", "creation_origin",
            )
        )
        line_movement_query = StockMovement.objects.filter(batch_line=line)
        before = _movement_snapshot(line_movement_query)
        lots, movements, foreign = _read_line(line, exclude_lot)
        own = _group_own_movements(movements, foreign)
        nearby_moves = _nearby_origin_movements(lots)
        nearby_transfers = _nearby_transfer_documents(lots, own)
        lots_after = list(
            lot_query.order_by("pk").values_list(
                "pk", "batch_id", "batch_line_id", "part_type_id", "location_id",
                "quantity", "initial_quantity", "status", "created_at", "origin_transfer_id",
                "origin_return_line_id", "creation_origin",
            )
        )
        after = _movement_snapshot(line_movement_query)
        nearby_after = _nearby_origin_movements(lots)
        transfers_after = _nearby_transfer_documents(lots, own)
        consistent_snapshot = (
            sorted(_lot_signature(lot) for lot in lots) == lots_before == lots_after
            and sorted(_movement_signature(m) for m in movements) == before == after
            and sorted(_movement_signature(m) for m in nearby_moves)
            == sorted(_movement_signature(m) for m in nearby_after)
            and sorted(
                (t.pk, t.part_type_id, t.quantity, t.from_location_id,
                 t.to_location_id, t.stock_state, t.created_at)
                for t in nearby_transfers
            ) == sorted(
                (t.pk, t.part_type_id, t.quantity, t.from_location_id,
                 t.to_location_id, t.stock_state, t.created_at)
                for t in transfers_after
            )
        )
        if consistent_snapshot:
            break
    if not consistent_snapshot:
        # Never classify an unstable mixture of lots and journal rows as
        # primary intake. A caller can retry the business operation later.
        uncertain = [
            LotProvenance(lot.pk, lot.batch_line_id, lot.status, UNKNOWN, None,
                          "журнал изменился во время проверки происхождения")
            for lot in lots
        ]
        return LineProvenance(uncertain, Decimal("0"), True)
    own = _group_own_movements(movements, foreign)
    unanchored_transfer_lots = _possible_unanchored_transfer_lots(lots, own)
    hinted_transfer_ids = {
        transfer_id
        for lot in lots
        if (transfer_id := (lot.origin_transfer_id or _transfer_note_id(lot))) is not None
    }
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
    transfer_ids.update(hinted_transfer_ids)
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
    source_lot_ids = {
        movement.stock_lot_id for movement in transfer_rows if movement.stock_lot_id
    }
    source_lots = StockLot.objects.in_bulk(source_lot_ids)
    source_history = StockMovement.objects.filter(
        stock_lot_id__in=source_lot_ids, batch_line__isnull=False
    ).values_list("stock_lot_id", "batch_line_id")
    conflicting_source_lots = {
        lot_id for lot_id, historical_line_id in source_history
        if lot_id in source_lots and historical_line_id != source_lots[lot_id].batch_line_id
    }
    lot_ids = {lot.pk for lot in lots}
    from apps.returns.models import StockReturnLine

    return_rows_by_lot = {}
    return_docs_by_lot = {
        lot_id: {
            movement.document_id
            for movement in own.get(lot_id, [])
            if movement.document_type == "stock_return" and movement.document_id is not None
        }
        for lot_id in lot_ids
    }
    return_line_ids = {
        lot.origin_return_line_id for lot in lots if lot.origin_return_line_id
    }
    origin_return_by_lot = {
        lot.pk: lot.origin_return_line_id for lot in lots if lot.origin_return_line_id
    }
    return_doc_ids = {
        document_id for document_ids in return_docs_by_lot.values()
        for document_id in document_ids
    }
    return_doc_ids.update(
        StockReturnLine.objects.filter(pk__in=return_line_ids)
        .values_list("stock_return_id", flat=True)
    )
    return_lines = list(
        StockReturnLine.objects.filter(
            Q(returned_lot_id__in=lot_ids)
            | Q(stock_return_id__in=return_doc_ids)
            | Q(pk__in=return_line_ids)
        )
        .select_related("stock_return")
        .order_by("pk")
    )
    return_doc_ids.update(line.stock_return_id for line in return_lines)
    return_movements = list(
        StockMovement.objects.filter(
            document_type="stock_return", document_id__in=return_doc_ids
        ).order_by("created_at", "pk")
    )
    return_doc_movements = {}
    for movement in return_movements:
        return_doc_movements.setdefault(movement.document_id, []).append(movement)
    for return_line in return_lines:
        for lot_id in lot_ids:
            if (
                return_line.returned_lot_id == lot_id
                or return_line.stock_return_id in return_docs_by_lot[lot_id]
                or return_line.pk == origin_return_by_lot.get(lot_id)
            ):
                return_rows_by_lot.setdefault(lot_id, []).append(return_line)
    return_movement_query = StockMovement.objects.filter(
        document_type="stock_return", document_id__in=return_doc_ids
    )
    return_line_query = StockReturnLine.objects.filter(
        Q(returned_lot_id__in=lot_ids)
        | Q(stock_return_id__in=return_doc_ids)
        | Q(pk__in=return_line_ids)
    )

    def return_line_signature(row):
        return (
            row.pk, row.stock_return_id, row.returned_lot_id, row.part_item_id,
            row.batch_id, row.batch_line_id, row.part_type_id, row.to_location_id,
            row.quantity, row.stock_return.status, row.stock_return.completed_at,
        )

    if (
        lots_after != list(
            lot_query.order_by("pk").values_list(
                "pk", "batch_id", "batch_line_id", "part_type_id", "location_id",
                "quantity", "initial_quantity", "status", "created_at", "origin_transfer_id",
                "origin_return_line_id", "creation_origin",
            )
        )
        or after != _movement_snapshot(line_movement_query)
        or sorted(_movement_signature(row) for row in return_movements)
        != _movement_snapshot(return_movement_query)
        or sorted(return_line_signature(row) for row in return_lines)
        != sorted(
            return_line_signature(row)
            for row in return_line_query.select_related("stock_return")
        )
    ):
        return LineProvenance(
            [
                LotProvenance(lot.pk, lot.batch_line_id, lot.status, UNKNOWN, None,
                              "документ возврата изменился во время проверки")
                for lot in lots
            ],
            Decimal("0"), True,
        )
    line_counts = BatchLine.objects.filter(
        batch_id__in={lot.batch_id for lot in lots},
        part_type_id__in={lot.part_type_id for lot in lots},
    ).values("batch_id", "part_type_id").annotate(n=Count("pk"))
    legacy_line_keys = {
        (row["batch_id"], row["part_type_id"])
        for row in line_counts
        if row["n"] == 1
    }
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
            transfer_movements, legacy_line_keys, return_rows_by_lot.get(lot.pk, ()),
            conflicting_source_lots, unanchored_transfer_lots, return_doc_movements,
        )
        for lot in lots
    ]
    return LineProvenance(rows, elsewhere, unproven_detached_history)


def line_provenance(line, *, exclude_lot=None) -> list[LotProvenance]:
    return line_provenance_detail(line, exclude_lot=exclude_lot).lots
