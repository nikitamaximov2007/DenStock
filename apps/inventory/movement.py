"""Read models for movement lookup and live stock presentation.

Physical truth is always ``StockLot`` plus physical ``PartItem`` rows. Counting
lines are historical snapshots and ``StockBalance`` is only a rebuildable cache;
neither is used here as current cell content.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, Sum

from apps.catalog.models import PartType

from .models import PartItem, StockLot, StockMovement, StockTransfer
from .presentation import manufacturer_display, part_exact_number, with_part_identity
from .services import ITEM_PHYSICAL_STATUSES, LOT_PHYSICAL_STATUSES

DEC0 = Decimal("0")


@dataclass
class MovementSource:
    part_type: PartType
    location: object
    stock_state: str
    physical: Decimal = DEC0
    available: Decimal = DEC0
    reserved: Decimal = DEC0
    quarantine: Decimal = DEC0
    movable: Decimal = DEC0
    batches: set[str] = field(default_factory=set)
    part_exact_number: str = ""
    part_manufacturer: str = ""

    @property
    def state_label(self) -> str:
        if self.stock_state == StockLot.Status.QUARANTINE:
            return "Карантин"
        if self.stock_state == "serial":
            return "Экземпляр"
        return "Доступный остаток"

    @property
    def batches_label(self) -> str:
        return ", ".join(sorted(self.batches))


@dataclass
class LiveStockRow:
    part_type: PartType
    location: object
    physical: Decimal = DEC0
    available: Decimal = DEC0
    reserved: Decimal = DEC0
    quarantine: Decimal = DEC0
    receiving: Decimal = DEC0
    batches: set[str] = field(default_factory=set)
    states: set[str] = field(default_factory=set)
    part_exact_number: str = ""
    part_manufacturer: str = ""

    @property
    def batches_label(self) -> str:
        return ", ".join(sorted(self.batches))

    @property
    def quantity_physical(self) -> Decimal:
        return self.physical

    @property
    def quantity_available(self) -> Decimal:
        return self.available

    @property
    def quantity_reserved(self) -> Decimal:
        return self.reserved

    @property
    def quantity_quarantine(self) -> Decimal:
        return self.quarantine

    @property
    def state_label(self) -> str:
        labels = []
        if StockLot.Status.AVAILABLE in self.states or PartItem.Status.AVAILABLE in self.states:
            labels.append("Доступный")
        if StockLot.Status.QUARANTINE in self.states or PartItem.Status.QUARANTINE in self.states:
            labels.append("Карантин")
        return ", ".join(labels)


def _identity_parts(part_ids) -> dict[int, PartType]:
    parts = list(
        with_part_identity(PartType.objects.filter(pk__in=part_ids), part_field="")
    )
    for part in parts:
        part.live_exact_number = part_exact_number(part, default="")
        part.live_manufacturer = manufacturer_display(part)
    return {part.pk: part for part in parts}


def _physical_lots(*, part_id=None, part_ids=None, batch_id=None, location_id=None):
    qs = StockLot.objects.filter(status__in=LOT_PHYSICAL_STATUSES, quantity__gt=0)
    if part_id:
        qs = qs.filter(part_type_id=part_id)
    if part_ids is not None:
        qs = qs.filter(part_type_id__in=part_ids)
    if batch_id:
        qs = qs.filter(batch_id=batch_id)
    if location_id:
        qs = qs.filter(location_id=location_id)
    return list(qs.select_related("location", "batch").order_by("created_at", "pk"))


def _physical_items(*, part_id=None, part_ids=None, batch_id=None, location_id=None):
    qs = PartItem.objects.filter(
        status__in=ITEM_PHYSICAL_STATUSES, current_location__isnull=False
    )
    if part_id:
        qs = qs.filter(part_type_id=part_id)
    if part_ids is not None:
        qs = qs.filter(part_type_id__in=part_ids)
    if batch_id:
        qs = qs.filter(batch_id=batch_id)
    if location_id:
        qs = qs.filter(current_location_id=location_id)
    return list(qs.select_related("current_location", "batch").order_by("created_at", "pk"))


def _reservation_maps(lots, items):
    # Local import keeps the established sales -> inventory app startup order acyclic.
    from apps.sales.services import active_reserved_for_lots, active_reserved_item_ids

    return active_reserved_for_lots(lots), active_reserved_item_ids(items)


def movement_sources_for_part(part: PartType) -> tuple[list[MovementSource], list[PartItem]]:
    """Live placed sources for an exact part identity.

    Bulk rows are grouped by physical location and stock state. Serial objects
    stay individually selectable because their internal barcode is identity.
    """
    lots = _physical_lots(part_id=part.pk)
    items = _physical_items(part_id=part.pk)
    reserved_lots, reserved_items = _reservation_maps(lots, items)

    grouped: dict[tuple[int, str], MovementSource] = {}
    for lot in lots:
        key = (lot.location_id, lot.status)
        row = grouped.setdefault(
            key,
            MovementSource(part_type=part, location=lot.location, stock_state=lot.status),
        )
        row.physical += lot.quantity
        row.batches.add(lot.batch.number)
        if lot.status == StockLot.Status.QUARANTINE:
            row.quarantine += lot.quantity
            row.movable += lot.quantity
        else:
            reserved = min(reserved_lots.get(lot.pk, DEC0), lot.quantity)
            row.reserved += reserved
            row.available += lot.quantity - reserved
            row.movable += lot.quantity - reserved

    exact = part_exact_number(part, default="")
    manufacturer = manufacturer_display(part)
    for row in grouped.values():
        row.part_exact_number = exact
        row.part_manufacturer = manufacturer

    for item in items:
        item.is_reserved_for_move = item.pk in reserved_items
        item.part_exact_number = exact
        item.part_manufacturer = manufacturer
    return (
        sorted(grouped.values(), key=lambda row: (row.location.code, row.stock_state)),
        items,
    )


def live_stock_rows(
    *, part_id=None, part_ids=None, batch_id=None, location_id=None
) -> list[LiveStockRow]:
    """Current physical stock grouped by exact part identity and cell."""
    lots = _physical_lots(
        part_id=part_id, part_ids=part_ids, batch_id=batch_id, location_id=location_id
    )
    items = _physical_items(
        part_id=part_id, part_ids=part_ids, batch_id=batch_id, location_id=location_id
    )
    reserved_lots, reserved_items = _reservation_maps(lots, items)
    grouped: dict[tuple[int, int], LiveStockRow] = {}

    def row_for(part_type_id, location):
        key = (part_type_id, location.pk)
        if key not in grouped:
            grouped[key] = LiveStockRow(part_type=None, location=location)
        return grouped[key]

    for lot in lots:
        row = row_for(lot.part_type_id, lot.location)
        row.physical += lot.quantity
        row.batches.add(lot.batch.number)
        row.states.add(lot.status)
        if lot.status == StockLot.Status.RECEIVING:
            row.receiving += lot.quantity
        elif lot.status == StockLot.Status.QUARANTINE:
            row.quarantine += lot.quantity
        else:
            reserved = min(reserved_lots.get(lot.pk, DEC0), lot.quantity)
            row.reserved += reserved
            row.available += lot.quantity - reserved

    for item in items:
        row = row_for(item.part_type_id, item.current_location)
        row.physical += Decimal("1")
        row.batches.add(item.batch.number)
        row.states.add(item.status)
        if item.status == PartItem.Status.RECEIVING:
            row.receiving += Decimal("1")
        elif item.status == PartItem.Status.QUARANTINE:
            row.quarantine += Decimal("1")
        elif item.pk in reserved_items:
            row.reserved += Decimal("1")
        else:
            row.available += Decimal("1")

    parts = _identity_parts({key[0] for key in grouped})
    for (part_type_id, _location_id), row in grouped.items():
        part = parts[part_type_id]
        row.part_type = part
        row.part_exact_number = part.live_exact_number
        row.part_manufacturer = part.live_manufacturer
    return sorted(
        grouped.values(),
        key=lambda row: (row.part_type.name.casefold(), row.location.code, row.part_type.pk),
    )


def unplaced_stock_exists(part: PartType) -> bool:
    item_exists = PartItem.objects.filter(
        part_type=part,
        status__in=ITEM_PHYSICAL_STATUSES,
        current_location__isnull=True,
    ).exists()
    return item_exists or StockLot.objects.filter(
        part_type=part, status=StockLot.Status.RECEIVING, quantity__gt=0
    ).exists()


def stock_location_consistency_issues() -> list[str]:
    """Read-only consistency audit for physical placement, cache, and transfers."""
    from .services import check_stock_balance

    issues = []
    for lot in StockLot.objects.filter(quantity__lt=0).values("pk", "quantity"):
        issues.append(f"negative lot id={lot['pk']} quantity={lot['quantity']}")
    unplaced_items = PartItem.objects.filter(
        status__in=ITEM_PHYSICAL_STATUSES, current_location__isnull=True
    ).values_list("pk", flat=True)
    for item_id in unplaced_items:
        issues.append(f"physical item without location id={item_id}")

    duplicates = (
        StockLot.objects.filter(status__in=LOT_PHYSICAL_STATUSES, quantity__gt=0)
        .values("batch_line_id", "location_id")
        .annotate(rows=Count("id"))
        .filter(rows__gt=1)
    )
    for row in duplicates:
        issues.append(
            "duplicate placement "
            f"batch_line={row['batch_line_id']} location={row['location_id']} rows={row['rows']}"
        )

    issues.extend(f"stock balance: {message}" for message in check_stock_balance())

    transfers = StockTransfer.objects.all().values(
        "pk", "quantity", "from_location_id", "to_location_id"
    )
    for transfer in transfers:
        ledger = StockMovement.objects.filter(
            document_type="stock_transfer", document_id=transfer["pk"]
        ).aggregate(quantity=Sum("quantity"), rows=Count("id"))
        if not ledger["rows"]:
            issues.append(f"transfer id={transfer['pk']} has no ledger rows")
            continue
        if (ledger["quantity"] or DEC0) != transfer["quantity"]:
            issues.append(
                f"transfer id={transfer['pk']} quantity={transfer['quantity']} "
                f"ledger={ledger['quantity']}"
            )
        mismatched = StockMovement.objects.filter(
            document_type="stock_transfer", document_id=transfer["pk"]
        ).exclude(
            from_location_id=transfer["from_location_id"],
            to_location_id=transfer["to_location_id"],
        )
        if mismatched.exists():
            issues.append(f"transfer id={transfer['pk']} location differs from ledger")

    orphan_transfer_movements = StockMovement.objects.filter(
        document_type="stock_transfer"
    ).exclude(document_id__in=StockTransfer.objects.values("pk"))
    for movement_id in orphan_transfer_movements.values_list("pk", flat=True):
        issues.append(f"orphan stock_transfer movement id={movement_id}")
    return issues
