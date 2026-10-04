"""Find stored piece quantities that are not whole numbers. Read only.

Covers customer documents (sales, requests, repairs, reservations, write-offs),
physical stock (lots, movements, transfers) and stock intake or correction
(receipts, batches, inventory counts, section recounts).

A part that is not oil is counted in pieces (apps.catalog.quantity_units), so a
stored 1.500 on such a part is a legacy anomaly from before the shared rule.
This module only reads: it never rounds, edits or deletes a row. The report
names rows by table, id, document id, status, part id, unit and quantity, and
never by customer name or phone.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, F
from django.db.models.functions import Floor


@dataclass(frozen=True)
class Source:
    label: str
    model_path: str
    quantity_field: str
    document_field: str  # "" when the row is its own document (a stock lot)
    status_path: str


SOURCES = (
    Source("Строки продаж", "sales.SaleLine", "quantity", "sale_id", "sale__status"),
    Source(
        "Строки заявок клиентов", "customer_requests.CustomerRequestLine",
        "quantity_requested", "request_id", "request__status",
    ),
    Source(
        "Строки ремонтов", "repairs.RepairIssueLine", "quantity",
        "repair_order_id", "repair_order__status",
    ),
    Source(
        "Строки броней", "sales.ReservationLine", "quantity",
        "reservation_id", "reservation__status",
    ),
    Source(
        "Строки списаний", "writeoffs.WriteOffLine", "quantity",
        "write_off_id", "write_off__status",
    ),
    Source("Остатки лотов", "inventory.StockLot", "quantity", "", "status"),
    Source(
        "Строки поступлений", "receipts.ReceiptLine", "quantity", "receipt_id", "receipt__status",
    ),
    Source("Строки партий", "procurement.BatchLine", "quantity", "batch_id", "batch__status"),
    Source("Перемещения", "inventory.StockTransfer", "quantity", "", "stock_state"),
    Source(
        "Факт инвентаризаций", "stocktaking.InventoryCountLine", "counted_quantity",
        "count_document_id", "count_document__status",
    ),
    Source(
        "Строки пересчёта участка", "stocktaking.SectionRecountLine", "quantity",
        "recount_id", "recount__status",
    ),
    Source("Движения склада", "inventory.StockMovement", "quantity", "", "movement_type"),
)


@dataclass(frozen=True)
class FractionalRow:
    source: str
    row_id: int
    document_id: int | None
    status: str
    part_type_id: int
    unit: str
    quantity: Decimal


@dataclass
class PieceQuantityReport:
    rows: list[FractionalRow] = field(default_factory=list)
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    piece_parts_by_unit: dict[str, int] = field(default_factory=dict)
    oil_parts_by_unit: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.rows)


def fractional_piece_rows(source: Source):
    """Rows of one table whose piece quantity is not a whole number."""
    from django.apps import apps

    model = apps.get_model(source.model_path)
    quantity = source.quantity_field
    values = ["pk", "part_type_id", "part_type__unit__short_name", quantity, source.status_path]
    if source.document_field:
        values.append(source.document_field)
    return (
        model.objects.filter(part_type__is_oil=False)
        .annotate(_whole=Floor(quantity))
        .exclude(**{quantity: F("_whole")})
        .order_by("pk")
        .values(*values)
    )


def audit_piece_quantities() -> PieceQuantityReport:
    from apps.catalog.models import PartType

    report = PieceQuantityReport()
    for source in SOURCES:
        statuses: dict[str, int] = {}
        for row in fractional_piece_rows(source):
            status = row[source.status_path] or ""
            statuses[status] = statuses.get(status, 0) + 1
            report.rows.append(
                FractionalRow(
                    source=source.label,
                    row_id=row["pk"],
                    document_id=row.get(source.document_field) if source.document_field else None,
                    status=status,
                    part_type_id=row["part_type_id"],
                    unit=row["part_type__unit__short_name"] or "",
                    quantity=row[source.quantity_field],
                )
            )
        report.by_source[source.label] = statuses
    for is_oil, target in ((False, report.piece_parts_by_unit), (True, report.oil_parts_by_unit)):
        for item in (
            PartType.objects.filter(is_oil=is_oil)
            .values("unit__short_name")
            .annotate(parts=Count("pk"))
            .order_by("unit__short_name")
        ):
            target[item["unit__short_name"] or "без единицы"] = item["parts"]
    return report
