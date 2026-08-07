"""Безопасная пересборка факта по фиксированному участку хранения.

Модуль намеренно не переиспользует scanner-ввод: тот создаёт новый приход.
Здесь сначала фиксируется snapshot, затем оператор вводит факт, а apply
проводит только разницу через inventory.adjust_stock_lot_quantity.
"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.db import IntegrityError, transaction
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
from apps.procurement.models import BatchLine
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


def _active_locations(section_code: str) -> list[StorageLocation]:
    codes = canonical_cell_codes(section_code)
    locations = list(StorageLocation.objects.filter(code__in=codes).order_by("code", "pk"))
    present = {location.code for location in locations}
    missing = [code for code in codes if code not in present]
    if missing != [f"{section_code}-C05"]:
        raise SectionRecountError(
            "Структура участка изменилась: ожидается единственная отсутствующая ячейка "
            f"C05, получено: {', '.join(missing) or 'ничего'}.")
    if missing:
        location = get_or_create_location(missing[0], name=missing[0])
        if not location.is_active or not location.storage_allowed:
            raise SectionRecountError("C05 существует, но недоступна для хранения.")
        locations.append(location)
    locations.sort(key=lambda item: item.code)
    if [item.code for item in locations] != codes:
        raise SectionRecountError("Нельзя построить ровно C01-C10 без дублей или пропусков.")
    return locations


def _capture_snapshot(locations: list[StorageLocation]) -> dict:
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
    preferred = list(
        PartPreferredLocation.objects.filter(location_id__in=location_ids)
        .values("part_type_id", "location_id")
        .order_by("part_type_id")
    )
    return {
        "section_code": locations[0].code.rsplit("-C", 1)[0],
        "locations": [
            {"id": location.pk, "code": location.code, "barcode": location.barcode}
            for location in locations
        ],
        "balances": [
            {
                "id": balance.pk,
                "location_id": balance.location_id,
                "part_type_id": balance.part_type_id,
                "batch_id": balance.batch_id,
                "batch_line_id": balance.batch_line_id,
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
            }
            for lot in lots
        ],
        "preferred": preferred,
    }


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
    doc.status = SectionRecount.Status.COUNTING
    doc.started_at = timezone.now()
    doc.snapshot = _capture_snapshot(locations)
    doc.save(update_fields=["status", "started_at", "snapshot", "updated_at"])
    SectionRecountCell.objects.bulk_create(
        [
            SectionRecountCell(recount=doc, location=location, sequence=number)
            for number, location in enumerate(locations, start=1)
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
                for location in locations
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
        BatchLine.objects.filter(part_type=line.part_type)
        .select_related("batch")
        .order_by("pk")
    )


@transaction.atomic
def allocate_section_line(line: SectionRecountLine, *, batch_line_id, quantity, by=None):
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
    if not batch_line.batch.cost_finalized:
        raise SectionRecountError("Себестоимость партии ещё не зафиксирована.")
    allocation, _ = SectionRecountAllocation.objects.update_or_create(
        line=line,
        batch_line=batch_line,
        defaults={"quantity": quantity, "unit_cost_rub": batch_line.landed_unit_cost_rub},
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
            if not candidate.batch.cost_finalized:
                continue
            SectionRecountAllocation.objects.create(
                line=line,
                batch_line=candidate,
                quantity=line.quantity,
                unit_cost_rub=candidate.landed_unit_cost_rub,
            )


def _validate_allocations(doc: SectionRecount) -> list[str]:
    warnings = []
    for line in doc.lines.prefetch_related("allocations"):
        total = sum((item.quantity for item in line.allocations.all()), Decimal("0"))
        if total != line.quantity:
            warnings.append(
                f"{line.part_number} / {line.cell.location.code}: "
                f"партии {total}, факт {line.quantity}"
            )
    return warnings


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
    _prepare_allocations(doc)
    warnings = _validate_allocations(doc)
    if warnings:
        raise SectionRecountError("Нужно явно распределить партии: " + "; ".join(warnings[:3]))
    doc.result = build_section_dry_run(doc)
    doc.status = SectionRecount.Status.READY
    doc.save(update_fields=["status", "result", "updated_at"])
    return doc


def _assert_snapshot_unchanged(doc: SectionRecount) -> None:
    expected = {
        item["id"]: (_decimal(item["quantity"]), item["status"])
        for item in doc.snapshot.get("lots", [])
    }
    current = {
        lot.pk: (lot.quantity, lot.status)
        for lot in StockLot.objects.filter(
            location_id__in=doc.cells.values("location_id"), quantity__gt=0
        )
    }
    if current != expected:
        raise SectionRecountError("Состояние участка изменилось после snapshot; apply остановлен.")


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
                allocation.batch_line, line.cell.location, section_recount_id=doc.pk
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
