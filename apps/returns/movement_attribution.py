"""Conservative ownership of legacy stock-return ledger movements.

Historically, a sale cancellation could write ``document_type=stock_return``
with the *sale* ID. A return with the same numeric ID therefore cannot claim a
movement from the document pointer alone. Every candidate is classified as
owned, proven unrelated sale cancellation, or ambiguous. Ambiguity is treated
as possible posted stock by mutation guards.
"""

from dataclasses import dataclass

from apps.inventory.models import StockMovement
from apps.sales.models import Sale, SaleLine

RETURN_MOVEMENT_TYPES = (
    StockMovement.MovementType.RETURN_ITEM,
    StockMovement.MovementType.RETURN_LOT,
)


@dataclass(frozen=True)
class ReturnMovementEvidence:
    owned_ids: tuple[int, ...] = ()
    unrelated_ids: tuple[int, ...] = ()
    ambiguous_ids: tuple[int, ...] = ()

    @property
    def possible_posting(self) -> bool:
        return bool(self.owned_ids or self.ambiguous_ids)


def _line_matches_return(ret, line, movement):
    if line.source_sale_line_id:
        source = line.source_sale_line
        source_matches = (
            ret.source_type == "sale" and source.sale_id == ret.source_id
            and line.quantity <= source.quantity
            and source.part_type_id == line.part_type_id
            and source.batch_line_id == line.batch_line_id
            and source.batch_id == line.batch_id
            and source.stock_lot_id == line.stock_lot_id
            and source.part_item_id == line.part_item_id
        )
    elif line.source_repair_line_id:
        source = line.source_repair_line
        source_matches = (
            ret.source_type == "repair_order" and source.repair_order_id == ret.source_id
            and line.quantity <= source.quantity
            and source.part_type_id == line.part_type_id
            and source.batch_line_id == line.batch_line_id
            and source.batch_id == line.batch_id
            and source.stock_lot_id == line.stock_lot_id
            and source.part_item_id == line.part_item_id
        )
    else:
        return False
    if not source_matches or (
        movement.comment != f"Возврат {ret.number}"
        or movement.part_type_id != line.part_type_id
        or movement.batch_id != line.batch_id
        or movement.batch_line_id != line.batch_line_id
        or movement.to_location_id != line.to_location_id
        or movement.from_location_id is not None
        or movement.quantity != line.quantity
    ):
        return False
    if line.part_item_id:
        return (
            movement.movement_type == StockMovement.MovementType.RETURN_ITEM
            and movement.part_item_id == line.part_item_id
            and movement.stock_lot_id is None
        )
    return (
        movement.movement_type == StockMovement.MovementType.RETURN_LOT
        and line.returned_lot_id is not None
        and movement.stock_lot_id == line.returned_lot_id
        and movement.part_item_id is None
    )


def _legacy_sale_cancellation(ret, movement, *, using):
    """Recognize the old writer using the canceled sale and its exact source row.

    Chronology is necessary but never sufficient: the sale must be canceled,
    its persisted line must identify the part/lot/batch, and the movement must
    carry the sale cancellation's canonical comment. Anything less is unknown.
    """
    if movement.created_at >= ret.created_at or (
        ret.source_type == "sale" and ret.source_id == movement.document_id
    ):
        return False
    sale = Sale.objects.using(using).filter(
        pk=movement.document_id,
        status__in=(Sale.Status.CANCELED, Sale.Status.VOIDED),
        canceled_at__isnull=False,
    ).first()
    if not sale or not (
        movement.created_at <= sale.canceled_at < ret.created_at
        and movement.comment.startswith(f"Отмена продажи {sale.number}:")
    ):
        return False
    source_filter = {
        "sale_id": sale.pk,
        "part_type_id": movement.part_type_id,
        "batch_id": movement.batch_id,
        "batch_line_id": movement.batch_line_id,
        "quantity__gte": movement.quantity,
    }
    if movement.movement_type == StockMovement.MovementType.RETURN_LOT:
        source_filter["stock_lot_id"] = movement.stock_lot_id
        source_filter["part_item_id__isnull"] = True
    else:
        source_filter["part_item_id"] = movement.part_item_id
        source_filter["stock_lot_id__isnull"] = True
    if not SaleLine.objects.using(using).filter(**source_filter).exists():
        return False
    original_type = (
        StockMovement.MovementType.SALE_LOT
        if movement.movement_type == StockMovement.MovementType.RETURN_LOT
        else StockMovement.MovementType.SALE_ITEM
    )
    original_filter = {
        "document_type": "sale", "document_id": sale.pk,
        "movement_type": original_type,
        "part_type_id": movement.part_type_id,
        "batch_id": movement.batch_id,
        "batch_line_id": movement.batch_line_id,
        "quantity__gte": movement.quantity,
        "created_at__lte": movement.created_at,
    }
    if movement.stock_lot_id:
        original_filter["stock_lot_id"] = movement.stock_lot_id
    else:
        original_filter["part_item_id"] = movement.part_item_id
    return StockMovement.objects.using(using).filter(**original_filter).exists()


def return_movement_evidence(ret, *, using=None):
    """Classify all RETURN_* rows sharing this return's numeric document ID.

    A structured match to exactly one persisted return line proves ownership.
    A separately proven, earlier sale cancellation proves non-ownership. Rows
    satisfying neither (or both conflicting claims) remain ambiguous and must
    block posting, destructive deletion, and mutation of possible history.
    """
    from .models import StockReturnLine

    using = using or ret._state.db or "default"
    movements = list(StockMovement.objects.using(using).filter(
        document_type="stock_return", document_id=ret.pk,
        movement_type__in=RETURN_MOVEMENT_TYPES,
    ).order_by("pk"))
    if not movements:
        return ReturnMovementEvidence()
    lines = list(StockReturnLine.objects.using(using).filter(stock_return_id=ret.pk)
                 .select_related("source_sale_line", "source_repair_line").order_by("pk"))
    owned, unrelated, ambiguous = [], [], []
    for movement in movements:
        matching = [line for line in lines if _line_matches_return(ret, line, movement)]
        return_claim = len(matching) == 1 and movement.created_at >= ret.created_at
        sale_claim = _legacy_sale_cancellation(ret, movement, using=using)
        if return_claim and not sale_claim:
            owned.append(movement.pk)
        elif sale_claim and not matching:
            unrelated.append(movement.pk)
        else:
            ambiguous.append(movement.pk)
    return ReturnMovementEvidence(tuple(owned), tuple(unrelated), tuple(ambiguous))
