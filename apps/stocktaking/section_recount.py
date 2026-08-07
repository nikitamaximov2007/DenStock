"""Безопасная пересборка факта по фиксированному участку хранения.

Модуль намеренно не переиспользует scanner-ввод: тот создаёт новый приход.
Здесь сначала фиксируется snapshot, затем оператор вводит факт, а apply
проводит только разницу через inventory.adjust_stock_lot_quantity.
"""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.part_lookup import clean_lookup_value, resolve_part_lookup
from apps.inventory.models import (
    PartPreferredLocation,
    StockBalance,
    StockLocationLock,
    StockLot,
)
from apps.inventory.presentation import part_exact_number
from apps.inventory.services import (
    adjust_stock_lot_quantity,
    get_or_create_section_recount_lot,
    set_preferred_part_location,
)
from apps.procurement.models import Batch, BatchLine
from apps.sales.models import Reservation, ReservationLine
from apps.warehouse.addresses import get_or_create_location
from apps.warehouse.models import StorageLocation

from .models import (
    SectionRecount,
    SectionRecountAllocation,
    SectionRecountCell,
    SectionRecountLine,
)

SECTION_RECOUNT_DOCUMENT = "section_recount"
SECTION_CODE = "S03-L03-D02"
CELL_COUNT = 10
PHYSICAL_LOT_STATUSES = [StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE]
VALID_BATCH_STATUSES = {
    Batch.Status.ACCEPTED,
    Batch.Status.COST_CALCULATED,
    Batch.Status.CLOSED,
}
ACTIVE_STATUSES = {
    SectionRecount.Status.DRAFT,
    SectionRecount.Status.COUNTING,
    SectionRecount.Status.READY,
    SectionRecount.Status.APPLYING,
}


class SectionRecountError(ValueError):
    """Ошибка workflow, которую можно показать оператору."""


def canonical_cell_codes(section_code: str = SECTION_CODE) -> list[str]:
    return [f"{section_code}-C{number:02d}" for number in range(1, CELL_COUNT + 1)]


def _decimal(value) -> Decimal:
    return Decimal(str(value or "0"))


def _part_number(part) -> str:
    return part_exact_number(part, default=str(part.pk))


def _fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _active_reservation_lines(location_ids):
    now = timezone.now()
    expiry = Q(reservation__expires_at__isnull=True) | Q(reservation__expires_at__gt=now)
    return list(
        ReservationLine.objects.filter(
            Q(stock_lot__location_id__in=location_ids)
            | Q(part_item__current_location_id__in=location_ids),
            reservation__status=Reservation.Status.ACTIVE,
        )
        .filter(expiry)
        .select_related(
            "reservation",
            "part_type",
            "stock_lot__location",
            "stock_lot__batch_line__batch",
            "part_item__current_location",
            "part_item__batch_line__batch",
        )
        .order_by("id")
    )


def _reservation_snapshot(lines) -> list[dict]:
    result = []
    for line in lines:
        lot = line.stock_lot
        item = line.part_item
        location = lot.location if lot is not None else item.current_location
        batch_line = lot.batch_line if lot is not None else item.batch_line
        result.append(
            {
                "id": line.id,
                "reservation_id": line.reservation_id,
                "reservation_number": line.reservation.number,
                "customer_name": line.reservation.customer_name,
                "part_type_id": line.part_type_id,
                "part_number": _part_number(line.part_type),
                "lot_id": lot.pk if lot is not None else None,
                "part_item_id": item.pk if item is not None else None,
                "batch_line_id": batch_line.pk if batch_line is not None else None,
                "location_id": location.pk if location is not None else None,
                "location_code": location.code if location is not None else "",
                "quantity": str(line.quantity),
            }
        )
    return result


def _format_reservations(reservations) -> str:
    return "; ".join(
        f"{item['part_number']} / лот {item['lot_id'] or item['part_item_id']} / "
        f"{item['location_code']} / зарезервировано {item['quantity']} / "
        f"заказ {item['reservation_number']} ({item['customer_name']})"
        for item in reservations
    )


def _active_locations(section_code: str) -> list[StorageLocation]:
    codes = canonical_cell_codes(section_code)
    locations = list(StorageLocation.objects.filter(code__in=codes).order_by("code", "pk"))
    present = {location.code for location in locations}
    missing = [code for code in codes if code not in present]
    if missing not in ([], [f"{section_code}-C05"]):
        raise SectionRecountError(
            "Структура участка изменилась: допускается только отсутствие C05, "
            f"получено: {', '.join(missing) or 'ничего'}."
        )
    if missing:
        location = get_or_create_location(missing[0], name=missing[0])
        if not location.is_active or not location.storage_allowed:
            raise SectionRecountError("C05 существует, но недоступна для хранения.")
        locations.append(location)
    locations.sort(key=lambda item: item.code)
    if [item.code for item in locations] != codes:
        raise SectionRecountError("Нельзя построить ровно C01-C10 без дублей или пропусков.")
    return locations


def _capture_snapshot(locations: list[StorageLocation], *, section_code=SECTION_CODE) -> dict:
    location_ids = [location.pk for location in locations]
    balances = list(
        StockBalance.objects.filter(location_id__in=location_ids)
        .select_related("part_type", "batch", "batch_line", "location")
        .order_by("location_id", "part_type_id", "batch_line_id")
    )
    lots = list(
        StockLot.objects.filter(location_id__in=location_ids, quantity__gt=0)
        .select_related("part_type", "batch", "batch_line", "location")
        .order_by("location_id", "part_type_id", "batch_line_id", "pk")
    )
    part_ids = {item.part_type_id for item in balances} | {item.part_type_id for item in lots}
    preferred = list(
        PartPreferredLocation.objects.filter(part_type_id__in=part_ids)
        .values("part_type_id", "location_id", "updated_at")
        .order_by("part_type_id")
    )
    for item in preferred:
        item["updated_at"] = item["updated_at"].isoformat()
    reservations = _reservation_snapshot(_active_reservation_lines(location_ids))
    snapshot = {
        "section_code": section_code,
        "locations": [
            {
                "id": location.pk,
                "code": location.code,
                "name": location.name,
                "barcode": location.barcode,
                "is_active": location.is_active,
                "storage_allowed": location.storage_allowed,
                "parent_id": location.parent_id,
            }
            for location in locations
        ],
        "balances": [
            {
                "id": balance.pk,
                "location_id": balance.location_id,
                "part_type_id": balance.part_type_id,
                "batch_id": balance.batch_id,
                "batch_line_id": balance.batch_line_id,
                "batch_status": balance.batch.status,
                "batch_cost_finalized": balance.batch.cost_finalized,
                "physical": str(balance.quantity_physical),
                "available": str(balance.quantity_available),
                "reserved": str(balance.quantity_reserved),
                "unit_cost_rub": str(balance.batch_line.landed_unit_cost_rub),
            }
            for balance in balances
        ],
        "lots": [
            {
                "id": lot.pk,
                "location_id": lot.location_id,
                "part_type_id": lot.part_type_id,
                "part_number": _part_number(lot.part_type),
                "batch_id": lot.batch_id,
                "batch_line_id": lot.batch_line_id,
                "quantity": str(lot.quantity),
                "available": lot.status == StockLot.Status.AVAILABLE,
                "status": lot.status,
                "unit_cost_rub": str(lot.landed_unit_cost_rub),
                "batch_status": lot.batch.status,
                "batch_cost_finalized": lot.batch.cost_finalized,
            }
            for lot in lots
        ],
        "preferred": preferred,
        "reservations": reservations,
    }
    snapshot["fingerprint"] = _fingerprint(snapshot)
    return snapshot


def create_section_recount(*, section_code=SECTION_CODE, by=None) -> SectionRecount:
    """Создать незапущенный документ без структуры и остатков."""
    if section_code != SECTION_CODE:
        raise SectionRecountError(f"Для этого workflow разрешён только участок {SECTION_CODE}.")
    try:
        with transaction.atomic():
            return SectionRecount.objects.create(
                section_code=section_code,
                operation_key=uuid4().hex,
                created_by=by,
            )
    except IntegrityError as exc:
        raise SectionRecountError("Для участка уже есть незавершённый пересчёт.") from exc


@transaction.atomic
def start_section_recount(doc: SectionRecount) -> SectionRecount:
    """Создать C05, snapshot и десять durable-lock записей."""
    doc = SectionRecount.objects.select_for_update().get(pk=doc.pk)
    if doc.status != SectionRecount.Status.DRAFT:
        if doc.status in ACTIVE_STATUSES:
            return doc
        raise SectionRecountError("Этот пересчёт уже закрыт.")
    locations = _active_locations(doc.section_code)
    locations = list(
        StorageLocation.objects.select_for_update()
        .filter(pk__in=[location.pk for location in locations])
        .order_by("pk")
    )
    if len(locations) != CELL_COUNT:
        raise SectionRecountError("Не удалось захватить все десять ячеек участка.")
    if (
        StockLocationLock.objects.filter(
            location_id__in=[location.pk for location in locations], released_at__isnull=True
        )
        .exclude(document_id=doc.pk)
        .exists()
    ):
        raise SectionRecountError("Участок уже заблокирован другой складской операцией.")
    codes = canonical_cell_codes(doc.section_code)
    if [location.code for location in sorted(locations, key=lambda item: item.code)] != codes:
        raise SectionRecountError("Структура участка изменилась до захвата блокировки.")
    if any(not location.can_hold_stock() for location in locations):
        raise SectionRecountError("Одна из ячеек участка недоступна для хранения.")
    ordered_locations = sorted(locations, key=lambda item: item.code)
    snapshot = _capture_snapshot(ordered_locations, section_code=doc.section_code)
    doc.status = SectionRecount.Status.COUNTING
    doc.started_at = timezone.now()
    doc.snapshot = snapshot
    doc.snapshot_fingerprint = snapshot["fingerprint"]
    doc.save(
        update_fields=["status", "started_at", "snapshot", "snapshot_fingerprint", "updated_at"]
    )
    SectionRecountCell.objects.bulk_create(
        [
            SectionRecountCell(recount=doc, location=location, sequence=number)
            for number, location in enumerate(ordered_locations, start=1)
        ]
    )
    try:
        StockLocationLock.objects.bulk_create(
            [
                StockLocationLock(
                    location=location,
                    section_code=doc.section_code,
                    document_id=doc.pk,
                )
                for location in ordered_locations
            ]
        )
    except IntegrityError as exc:
        raise SectionRecountError(
            "Участок уже заблокирован другой складской операцией."
        ) from exc
    return doc


def _get_cell(doc, cell_number: int) -> SectionRecountCell:
    try:
        return doc.cells.select_related("location").get(sequence=cell_number)
    except SectionRecountCell.DoesNotExist as exc:
        raise SectionRecountError("Ячейка не входит в этот пересчёт.") from exc


def _ensure_counting(doc: SectionRecount) -> None:
    if doc.status not in {SectionRecount.Status.COUNTING, SectionRecount.Status.READY}:
        raise SectionRecountError("В этом статусе строки пересчёта изменять нельзя.")


def _reopen_ready(doc: SectionRecount) -> None:
    if doc.status == SectionRecount.Status.READY:
        doc.status = SectionRecount.Status.COUNTING
        doc.result = {}
        doc.save(update_fields=["status", "result", "updated_at"])


def _resolve_exact_part(raw):
    value = clean_lookup_value(raw)
    result = resolve_part_lookup(value)
    if result.status != "found" or result.candidate is None:
        raise SectionRecountError(result.message or "Нужно указать один точный артикул детали.")
    return result.candidate.part, _part_number(result.candidate.part)


@transaction.atomic
def record_section_scan(doc: SectionRecount, *, cell_number: int, raw_value: str, by=None):
    doc = SectionRecount.objects.select_for_update().get(pk=doc.pk)
    _ensure_counting(doc)
    _reopen_ready(doc)
    cell = _get_cell(doc, cell_number)
    part, part_number = _resolve_exact_part(raw_value)
    line = (
        SectionRecountLine.objects.select_for_update()
        .filter(recount=doc, cell=cell, part_type=part)
        .first()
    )
    if line is None:
        line = SectionRecountLine.objects.create(
            recount=doc, cell=cell, part_type=part, part_number=part_number, quantity=1
        )
    else:
        line.quantity += Decimal("1")
        line.save(update_fields=["quantity", "updated_at"])
    if cell.status == SectionRecountCell.Status.COMPLETED:
        cell.status = SectionRecountCell.Status.COUNTING
        cell.save(update_fields=["status"])
    return line


@transaction.atomic
def set_section_line_quantity(line: SectionRecountLine, quantity, *, by=None):
    line = (
        SectionRecountLine.objects.select_for_update()
        .select_related("recount", "cell")
        .get(pk=line.pk)
    )
    _ensure_counting(line.recount)
    _reopen_ready(line.recount)
    try:
        value = Decimal(str(quantity).replace(",", "."))
    except (InvalidOperation, TypeError) as exc:
        raise SectionRecountError("Количество должно быть числом.") from exc
    if value < 0:
        raise SectionRecountError("Количество не может быть отрицательным.")
    line.quantity = value
    line.save(update_fields=["quantity", "updated_at"])
    if line.cell.status == SectionRecountCell.Status.COMPLETED:
        line.cell.status = SectionRecountCell.Status.COUNTING
        line.cell.save(update_fields=["status"])
    return line


@transaction.atomic
def remove_section_line(line: SectionRecountLine, *, by=None) -> None:
    line = SectionRecountLine.objects.select_for_update().select_related("recount").get(pk=line.pk)
    _ensure_counting(line.recount)
    _reopen_ready(line.recount)
    line.delete()


@transaction.atomic
def complete_section_cell(doc: SectionRecount, *, cell_number: int, by=None):
    doc = SectionRecount.objects.select_for_update().get(pk=doc.pk)
    _ensure_counting(doc)
    _reopen_ready(doc)
    cell = _get_cell(doc, cell_number)
    cell.status = SectionRecountCell.Status.COMPLETED
    cell.counted_at = timezone.now()
    cell.save(update_fields=["status", "counted_at"])
    return cell


def _candidate_batch_lines(line: SectionRecountLine):
    return list(
        BatchLine.objects.filter(
            part_type=line.part_type,
            batch__status__in=VALID_BATCH_STATUSES,
            batch__cost_finalized=True,
        )
        .select_related("batch")
        .order_by("pk")
    )


def _snapshot_source_quantities(doc: SectionRecount) -> dict[tuple[int, str], Decimal]:
    quantities = defaultdict(Decimal)
    for item in doc.snapshot.get("lots", []):
        if item["status"] in PHYSICAL_LOT_STATUSES:
            quantities[(item["batch_line_id"], item["status"])] += _decimal(item["quantity"])
    return quantities


def _snapshot_source_costs(doc: SectionRecount) -> dict[tuple[int, str], Decimal]:
    costs = {}
    for item in doc.snapshot.get("lots", []):
        if item["status"] in PHYSICAL_LOT_STATUSES and _decimal(item["quantity"]) > 0:
            costs[(item["batch_line_id"], item["status"])] = _decimal(item["unit_cost_rub"])
    return costs


def _source_statuses(doc: SectionRecount, batch_line_id: int) -> list[str]:
    return sorted(
        status
        for (source_batch_line_id, status), quantity in _snapshot_source_quantities(doc).items()
        if source_batch_line_id == batch_line_id and quantity > 0
    )


def _allocation_summary(doc: SectionRecount) -> dict[tuple[int, str], Decimal]:
    totals = defaultdict(Decimal)
    for item in SectionRecountAllocation.objects.filter(
        line__recount=doc, quantity__gt=0
    ):
        totals[(item.batch_line_id, item.lot_status)] += item.quantity
    return totals


@transaction.atomic
def allocate_section_line(
    line: SectionRecountLine, *, batch_line_id, quantity, lot_status, by=None
):
    line = (
        SectionRecountLine.objects.select_for_update()
        .select_related("recount", "part_type")
        .get(pk=line.pk)
    )
    _ensure_counting(line.recount)
    _reopen_ready(line.recount)
    try:
        quantity = Decimal(str(quantity).replace(",", "."))
    except (InvalidOperation, TypeError) as exc:
        raise SectionRecountError("Количество партии должно быть числом.") from exc
    if quantity <= 0:
        raise SectionRecountError("Распределение партии должно быть больше нуля.")
    batch_line = BatchLine.objects.select_related("part_type").get(pk=batch_line_id)
    if batch_line.part_type_id != line.part_type_id:
        raise SectionRecountError("Партия не относится к этой детали.")
    if batch_line.batch.status not in VALID_BATCH_STATUSES or not batch_line.batch.cost_finalized:
        raise SectionRecountError("Партия не разрешена для складского остатка.")
    if lot_status not in PHYSICAL_LOT_STATUSES:
        raise SectionRecountError("Недопустимый статус лота для пересчёта.")
    source_quantity = _snapshot_source_quantities(line.recount).get(
        (batch_line.pk, lot_status), Decimal("0")
    )
    if source_quantity <= 0:
        raise SectionRecountError(
            "Для выбранной партии и статуса нет подтверждённого исходного лота "
            "в snapshot. Создание остатка из воздуха запрещено."
        )
    source_cost = _snapshot_source_costs(line.recount).get((batch_line.pk, lot_status))
    if source_cost is None:
        raise SectionRecountError("Для выбранной партии нет подтверждённой себестоимости.")
    existing = SectionRecountAllocation.objects.filter(
        line__recount=line.recount,
        batch_line=batch_line,
        lot_status=lot_status,
    ).exclude(line=line)
    allocated_elsewhere = existing.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    if allocated_elsewhere + quantity > source_quantity:
        raise SectionRecountError(
            f"Распределение {allocated_elsewhere + quantity} превышает snapshot-остаток "
            f"партии/статуса {source_quantity}."
        )
    allocation, _ = SectionRecountAllocation.objects.update_or_create(
        line=line,
        batch_line=batch_line,
        defaults={
            "quantity": quantity,
            "unit_cost_rub": source_cost,
            "lot_status": lot_status,
        },
    )
    return allocation


def _prepare_allocations(doc: SectionRecount) -> None:
    """Автоматизировать только действительно однозначные batch-line."""
    for line in doc.lines.select_related("part_type").prefetch_related("allocations"):
        if line.quantity <= 0 or line.allocations.exists():
            continue
        candidates = _candidate_batch_lines(line)
        if len(candidates) == 1:
            candidate = candidates[0]
            statuses = _source_statuses(doc, candidate.pk)
            if len(statuses) != 1:
                continue
            SectionRecountAllocation.objects.create(
                line=line,
                batch_line=candidate,
                quantity=line.quantity,
                unit_cost_rub=_snapshot_source_costs(doc)[(candidate.pk, statuses[0])],
                lot_status=statuses[0],
            )


def _validate_allocations(doc: SectionRecount) -> list[str]:
    warnings = []
    source_quantities = _snapshot_source_quantities(doc)
    source_costs = _snapshot_source_costs(doc)
    totals = defaultdict(Decimal)
    for line in doc.lines.prefetch_related(
        "allocations__batch_line__batch", "allocations__batch_line__part_type"
    ):
        total = sum((item.quantity for item in line.allocations.all()), Decimal("0"))
        if total != line.quantity:
            warnings.append(
                f"{line.part_number} / {line.cell.location.code}: "
                f"партии {total}, факт {line.quantity}"
            )
        for allocation in line.allocations.all():
            key = (allocation.batch_line_id, allocation.lot_status)
            totals[key] += allocation.quantity
            if allocation.batch_line.part_type_id != line.part_type_id:
                warnings.append(f"{line.part_number}: партия относится к другой детали")
            if allocation.lot_status not in PHYSICAL_LOT_STATUSES:
                warnings.append(f"{line.part_number}: недопустимый статус лота")
            if (
                allocation.batch_line.batch.status not in VALID_BATCH_STATUSES
                or not allocation.batch_line.batch.cost_finalized
            ):
                warnings.append(f"{line.part_number}: партия недоступна для склада")
            if allocation.unit_cost_rub != source_costs.get(key):
                warnings.append(f"{line.part_number}: себестоимость не совпадает с snapshot")
            if key not in source_quantities:
                warnings.append(f"{line.part_number}: исходный лот отсутствует в snapshot")
            if totals[key] > source_quantities.get(key, Decimal("0")):
                warnings.append(
                    f"партия {allocation.batch_line_id} / {allocation.lot_status}: "
                    "распределение превышает snapshot-остаток"
                )
    return warnings


def _validate_snapshot_lots(doc: SectionRecount) -> None:
    invalid_statuses = sorted(
        {item["status"] for item in doc.snapshot.get("lots", [])}
        - set(PHYSICAL_LOT_STATUSES)
    )
    if invalid_statuses:
        raise SectionRecountError(
            "В snapshot есть лоты в неподдерживаемом статусе: "
            + ", ".join(invalid_statuses)
        )


def _snapshot_totals(doc: SectionRecount):
    current = defaultdict(lambda: defaultdict(Decimal))
    for item in doc.snapshot.get("lots", []):
        current[item["location_id"]][item["part_type_id"]] += _decimal(item["quantity"])
    return current


def build_section_dry_run(doc: SectionRecount) -> dict:
    """Построить отчёт без записи в БД, включая все расхождения."""
    current = _snapshot_totals(doc)
    counted = defaultdict(lambda: defaultdict(Decimal))
    for line in doc.lines.select_related("cell").all():
        counted[line.cell.location_id][line.part_type_id] += line.quantity
    cell_rows = []
    all_keys = set(current) | set(counted)
    for location_id in sorted(all_keys):
        part_ids = set(current[location_id]) | set(counted[location_id])
        changes = []
        for part_id in sorted(part_ids):
            before = current[location_id][part_id]
            after = counted[location_id][part_id]
            if before != after:
                changes.append(
                    {"part_type_id": part_id, "before": str(before), "after": str(after)}
                )
        cell_rows.append({"location_id": location_id, "changes": changes})

    before_total = sum(
        (quantity for rows in current.values() for quantity in rows.values()), Decimal("0")
    )
    after_total = sum(
        (quantity for rows in counted.values() for quantity in rows.values()), Decimal("0")
    )
    part_before = defaultdict(Decimal)
    part_after = defaultdict(Decimal)
    for rows in current.values():
        for part_id, quantity in rows.items():
            part_before[part_id] += quantity
    for rows in counted.values():
        for part_id, quantity in rows.items():
            part_after[part_id] += quantity
    moved = [
        {"part_type_id": part_id, "quantity": str(part_after[part_id])}
        for part_id in sorted(set(part_before) & set(part_after))
        if part_before[part_id] == part_after[part_id]
        and any(current[cell][part_id] != counted[cell][part_id] for cell in all_keys)
    ]
    warnings = _validate_allocations(doc)
    unresolved = sum(
        1 for line in doc.lines.all() if line.quantity > 0 and not line.allocations.exists()
    )
    allocation_count = sum(
        1
        for line in doc.lines.prefetch_related("allocations")
        for item in line.allocations.all()
        if item.quantity > 0
    )
    multi_batch_parts = len({
        line.part_type_id
        for line in doc.lines.all()
        if len(_candidate_batch_lines(line)) > 1
    })
    allocation_rows = [
        {
            "line_id": allocation.line_id,
            "cell": allocation.line.cell.location.code,
            "part_number": allocation.line.part_number,
            "batch_line_id": allocation.batch_line_id,
            "status": allocation.lot_status,
            "quantity": str(allocation.quantity),
            "unit_cost_rub": str(allocation.unit_cost_rub),
        }
        for allocation in SectionRecountAllocation.objects.filter(
            line__recount=doc
        ).select_related("line__cell__location")
    ]
    return {
        "section_code": doc.section_code,
        "before_total": str(before_total),
        "after_total": str(after_total),
        "difference": str(after_total - before_total),
        "cell_rows": cell_rows,
        "moved": moved,
        "adjust_out": len(doc.snapshot.get("lots", [])),
        "adjust_in": allocation_count,
        "touched_parts": len(set(part_before) | set(part_after)),
        "multi_batch_parts": multi_batch_parts,
        "unresolved_allocations": unresolved,
        "warnings": warnings,
        "allocations": allocation_rows,
        "reservation_conflicts": doc.snapshot.get("reservations", []),
    }


@transaction.atomic
def mark_section_ready(doc: SectionRecount) -> SectionRecount:
    doc = SectionRecount.objects.select_for_update().get(pk=doc.pk)
    if doc.status != SectionRecount.Status.COUNTING:
        raise SectionRecountError("К dry-run можно перейти только из режима пересчёта.")
    if doc.cells.filter(
        status__in=[SectionRecountCell.Status.NOT_STARTED, SectionRecountCell.Status.COUNTING]
    ).exists():
        raise SectionRecountError("Сначала завершите каждую из 10 ячеек.")
    _assert_snapshot_unchanged(doc)
    if doc.snapshot.get("reservations"):
        raise SectionRecountError(
            "Участок нельзя подготовить: активные резервы должны быть разобраны вручную: "
            + _format_reservations(doc.snapshot["reservations"])
        )
    invalid_statuses = sorted(
        {item["status"] for item in doc.snapshot.get("lots", [])}
        - set(PHYSICAL_LOT_STATUSES)
    )
    if invalid_statuses:
        raise SectionRecountError(
            "В snapshot есть лоты в неподдерживаемом статусе: "
            + ", ".join(invalid_statuses)
        )
    _prepare_allocations(doc)
    warnings = _validate_allocations(doc)
    if warnings:
        raise SectionRecountError("Нужно явно распределить партии: " + "; ".join(warnings[:3]))
    doc.result = build_section_dry_run(doc)
    doc.status = SectionRecount.Status.READY
    doc.save(update_fields=["status", "result", "updated_at"])
    return doc


def _assert_snapshot_unchanged(doc: SectionRecount) -> None:
    locations = list(
        StorageLocation.objects.filter(pk__in=doc.cells.values("location_id"))
        .order_by("code", "pk")
    )
    live = _capture_snapshot(locations, section_code=doc.section_code)
    if live["fingerprint"] == doc.snapshot_fingerprint:
        return
    if live.get("reservations") != doc.snapshot.get("reservations"):
        details = _format_reservations(live.get("reservations", [])) or "резервов нет"
        raise SectionRecountError(
            "Snapshot mismatch: активные резервы появились или изменились: " + details
        )
    if live.get("preferred") != doc.snapshot.get("preferred"):
        raise SectionRecountError(
            "Snapshot mismatch: изменилась предпочтительная ячейка детали."
        )
    if live.get("locations") != doc.snapshot.get("locations"):
        raise SectionRecountError("Snapshot mismatch: изменилась структура или доступность ячейки.")
    raise SectionRecountError("Snapshot mismatch: состояние участка изменилось; apply остановлен.")


def _reconcile_preferred(doc: SectionRecount) -> dict:
    old = {item["part_type_id"]: item["location_id"] for item in doc.snapshot.get("preferred", [])}
    part_ids = set(doc.lines.values_list("part_type_id", flat=True))
    locations_by_part = defaultdict(set)
    for lot in StockLot.objects.filter(
        location_id__in=doc.cells.values("location_id"),
        part_type_id__in=part_ids,
        quantity__gt=0,
        status__in=PHYSICAL_LOT_STATUSES,
    ):
        locations_by_part[lot.part_type_id].add(lot.location_id)
    changed = saved = ambiguous = zero = 0
    for part_id in part_ids:
        locations = locations_by_part[part_id]
        if len(locations) == 1:
            location_id = next(iter(locations))
            if old.get(part_id) != location_id:
                set_preferred_part_location(
                    doc.lines.filter(part_type_id=part_id).first().part_type,
                    StorageLocation.objects.get(pk=location_id),
                    by=doc.created_by,
                    section_recount_id=doc.pk,
                )
                changed += 1
            else:
                saved += 1
        elif len(locations) > 1:
            ambiguous += 1
        else:
            zero += 1
    return {"changed": changed, "saved": saved, "ambiguous": ambiguous, "zero": zero}


def _apply_section_recount_atomic(doc_id: int) -> SectionRecount:
    doc = SectionRecount.objects.select_for_update().get(pk=doc_id)
    if doc.status == SectionRecount.Status.COMPLETED:
        return doc
    if doc.status != SectionRecount.Status.READY:
        raise SectionRecountError("Применить можно только документ в статусе READY.")
    _validate_snapshot_lots(doc)
    warnings = _validate_allocations(doc)
    if warnings:
        raise SectionRecountError(
            "Проверка распределения партий не пройдена: " + "; ".join(warnings[:3])
        )
    doc.status = SectionRecount.Status.APPLYING
    doc.save(update_fields=["status", "updated_at"])
    _assert_snapshot_unchanged(doc)
    lots = list(
        StockLot.objects.select_for_update().filter(
            location_id__in=doc.cells.values("location_id"), quantity__gt=0
        )
    )
    for lot in lots:
        adjust_stock_lot_quantity(
            lot, -lot.quantity, by=doc.created_by,
            comment=f"Пересчёт участка {doc.section_code} #{doc.pk}",
            document_type=SECTION_RECOUNT_DOCUMENT, document_id=doc.pk,
            section_recount_id=doc.pk, update_preferred=False,
        )
    for line in doc.lines.prefetch_related("allocations").select_related("cell", "part_type"):
        for allocation in line.allocations.all():
            lot = get_or_create_section_recount_lot(
                allocation.batch_line,
                line.cell.location,
                lot_status=allocation.lot_status,
                section_recount_id=doc.pk,
            )
            adjust_stock_lot_quantity(
                lot, allocation.quantity, by=doc.created_by,
                comment=f"Пересчёт участка {doc.section_code} #{doc.pk}",
                document_type=SECTION_RECOUNT_DOCUMENT, document_id=doc.pk,
                section_recount_id=doc.pk, update_preferred=False,
            )
    result = dict(doc.result or build_section_dry_run(doc))
    result["preferred"] = _reconcile_preferred(doc)
    result["created_movements"] = len(
        lots
    ) + SectionRecountAllocation.objects.filter(line__recount=doc, quantity__gt=0).count()
    doc.result = result
    doc.status = SectionRecount.Status.COMPLETED
    doc.completed_at = timezone.now()
    doc.save(update_fields=["status", "completed_at", "result", "updated_at"])
    StockLocationLock.objects.filter(document_id=doc.pk, released_at__isnull=True).update(
        released_at=timezone.now()
    )
    return doc


def apply_section_recount(doc: SectionRecount) -> SectionRecount:
    """Apply once; failed transactions become FAILED and keep the lock."""
    try:
        with transaction.atomic():
            return _apply_section_recount_atomic(doc.pk)
    except Exception as exc:
        SectionRecount.objects.filter(pk=doc.pk, status=SectionRecount.Status.READY).update(
            status=SectionRecount.Status.FAILED,
            error_message=str(exc)[:500],
            failed_at=timezone.now(),
        )
        if isinstance(exc, SectionRecountError):
            raise
        raise SectionRecountError("Применение пересчёта отменено и полностью откатилось.") from exc


@transaction.atomic
def cancel_section_recount(doc: SectionRecount) -> SectionRecount:
    doc = SectionRecount.objects.select_for_update().get(pk=doc.pk)
    if doc.status not in {
        SectionRecount.Status.DRAFT,
        SectionRecount.Status.COUNTING,
        SectionRecount.Status.READY,
        SectionRecount.Status.FAILED,
    }:
        raise SectionRecountError("Пересчёт нельзя отменить после начала apply.")
    doc.status = SectionRecount.Status.CANCELED
    doc.canceled_at = timezone.now()
    doc.save(update_fields=["status", "canceled_at", "updated_at"])
    StockLocationLock.objects.filter(document_id=doc.pk, released_at__isnull=True).update(
        released_at=timezone.now()
    )
    return doc
