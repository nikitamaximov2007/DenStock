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

from apps.catalog.models import PartType
from apps.core.part_lookup import clean_lookup_value, resolve_part_lookup
from apps.inventory.models import (
    PartItem,
    PartPreferredLocation,
    StockBalance,
    StockLocationLock,
    StockLot,
)
from apps.inventory.presentation import part_exact_number, with_part_identity
from apps.inventory.services import (
    ITEM_PHYSICAL_STATUSES,
    adjust_stock_lot_quantity,
    get_or_create_section_recount_lot,
    set_preferred_part_location,
)
from apps.procurement.models import Batch, BatchLine
from apps.sales.models import Reservation, ReservationLine
from apps.warehouse.addresses import (
    AddressError,
    get_or_create_location,
    parse_address,
)
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
    try:
        parsed = parse_address(section_code)
    except AddressError:
        parsed = None
    if parsed is not None and parsed.drawer is not None and parsed.cell is None:
        drawer = StorageLocation.objects.filter(
            code=parsed.code,
            level=StorageLocation.Level.DRAWER,
            is_active=True,
        ).first()
        if drawer is None:
            return []
        return list(
            drawer.children.filter(
                level=StorageLocation.Level.CELL,
                is_active=True,
                storage_allowed=True,
            )
            .order_by("code", "pk")
            .values_list("code", flat=True)
        )
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
    try:
        parsed = parse_address(section_code)
    except AddressError:
        parsed = None
    if parsed is not None and parsed.drawer is not None and parsed.cell is None:
        if not codes:
            raise SectionRecountError(
                "В выбранном V2-ящике нет активных ячеек, доступных для хранения."
            )
        locations = list(
            StorageLocation.objects.filter(code__in=codes)
            .select_related("parent")
            .order_by("code", "pk")
        )
        if any(
            location.parent is None
            or location.parent.code != section_code
            or location.level != StorageLocation.Level.CELL
            for location in locations
        ):
            raise SectionRecountError("Иерархия V2-ящика не соответствует его адресам.")
        if [location.code for location in locations] != codes:
            raise SectionRecountError("Структура V2-ячеек изменилась во время проверки.")
        return locations
    locations = list(StorageLocation.objects.filter(code__in=codes).order_by("code", "pk"))
    present = {location.code for location in locations}
    missing = [code for code in codes if code not in present]
    if missing not in ([], [f"{section_code}-C05"]):
        raise SectionRecountError(
            "Структура участка изменилась: допускается только отсутствие C05, "
            f"получено: {', '.join(missing) or 'ничего'}."
        )
    if missing:
        location = get_or_create_location(missing[0], name=missing[0], allow_legacy=True)
        if not location.is_active or not location.storage_allowed:
            raise SectionRecountError("C05 существует, но недоступна для хранения.")
        locations.append(location)
    locations.sort(key=lambda item: item.code)
    if [item.code for item in locations] != codes:
        raise SectionRecountError("Нельзя построить ровно C01-C10 без дублей или пропусков.")
    return locations


def _capture_snapshot(
    locations: list[StorageLocation], *, section_code=SECTION_CODE, scope="section"
) -> dict:
    location_ids = [location.pk for location in locations]
    balances = list(
        StockBalance.objects.filter(location_id__in=location_ids)
        .select_related("part_type", "batch", "batch_line", "location")
        .order_by("location_id", "part_type_id", "batch_line_id")
    )
    lots = list(
        with_part_identity(
            StockLot.objects.filter(location_id__in=location_ids, quantity__gt=0)
            .select_related("part_type", "batch", "batch_line", "location"),
            part_field="part_type",
        ).order_by("location_id", "part_type_id", "batch_line_id", "pk")
    )
    part_ids = {item.part_type_id for item in balances} | {item.part_type_id for item in lots}
    preferred = list(
        PartPreferredLocation.objects.filter(
            Q(part_type_id__in=part_ids) | Q(location_id__in=location_ids)
        )
        .values("part_type_id", "location_id", "updated_at")
        .order_by("part_type_id")
    )
    for item in preferred:
        item["updated_at"] = item["updated_at"].isoformat()
    reservations = _reservation_snapshot(_active_reservation_lines(location_ids))
    snapshot = {
        "section_code": section_code,
        "scope": scope,
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
                "batch_line_quantity": str(lot.batch_line.quantity),
                "batch_line_landed_unit_cost_rub": str(
                    lot.batch_line.landed_unit_cost_rub
                ),
                "batch_line_updated_at": lot.batch_line.updated_at.isoformat(),
                "batch_updated_at": lot.batch.updated_at.isoformat(),
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
    section_code = (section_code or "").strip().upper()
    if section_code != SECTION_CODE:
        try:
            parsed = parse_address(section_code)
        except AddressError as exc:
            raise SectionRecountError("Выберите canonical V2-ящик Sxx-Dxx.") from exc
        if parsed.drawer is None or parsed.cell is not None:
            raise SectionRecountError("Пересчёт участка запускается для V2-ящика Sxx-Dxx.")
        if not canonical_cell_codes(parsed.code):
            raise SectionRecountError(
                "В выбранном V2-ящике нет активных ячеек, доступных для хранения."
            )
    try:
        with transaction.atomic():
            return SectionRecount.objects.create(
                section_code=section_code,
                scope=SectionRecount.Scope.SECTION,
                operation_key=uuid4().hex,
                created_by=by,
            )
    except IntegrityError as exc:
        raise SectionRecountError("Для участка уже есть незавершённый пересчёт.") from exc


def create_cell_recount(*, location: StorageLocation, by=None) -> SectionRecount:
    """Создать, захватить и сразу запустить пересчёт одной ячейки."""
    try:
        with transaction.atomic():
            location = StorageLocation.objects.select_for_update().get(pk=location.pk)
            if not location.can_hold_stock():
                raise SectionRecountError("Пересчитать можно только активную складскую ячейку.")
            doc = SectionRecount.objects.create(
                section_code=location.code,
                scope=SectionRecount.Scope.CELL,
                operation_key=uuid4().hex,
                created_by=by,
            )
            SectionRecountCell.objects.create(recount=doc, location=location, sequence=1)
            return start_section_recount(doc)
    except IntegrityError as exc:
        raise SectionRecountError("Для этой ячейки уже есть незавершённый пересчёт.") from exc


def cell_recount_preview(location: StorageLocation) -> dict:
    lots = list(
        StockLot.objects.filter(
            location=location, status__in=PHYSICAL_LOT_STATUSES, quantity__gt=0
        ).values("part_type_id", "quantity")
    )
    items = list(
        PartItem.objects.filter(
            current_location=location, status__in=ITEM_PHYSICAL_STATUSES
        ).values_list("part_type_id", flat=True)
    )
    return {
        "position_count": len({item["part_type_id"] for item in lots} | set(items)),
        "unit_count": sum((item["quantity"] for item in lots), Decimal("0"))
        + Decimal(len(items)),
        "serial_count": len(items),
    }


def _recount_locations(doc: SectionRecount) -> tuple[list[StorageLocation], bool]:
    if doc.is_cell_recount:
        locations = list(doc.cells.select_related("location").values_list("location_id", flat=True))
        if len(locations) != 1:
            raise SectionRecountError("Пересчёт ячейки должен содержать ровно одну цель.")
        return list(StorageLocation.objects.filter(pk__in=locations)), False
    return _active_locations(doc.section_code), True


@transaction.atomic
def start_section_recount(doc: SectionRecount) -> SectionRecount:
    """Зафиксировать цели, snapshot и durable-lock записи."""
    doc = SectionRecount.objects.select_for_update().get(pk=doc.pk)
    if doc.status != SectionRecount.Status.DRAFT:
        if doc.status in ACTIVE_STATUSES:
            return doc
        raise SectionRecountError("Этот пересчёт уже закрыт.")
    locations, create_cells = _recount_locations(doc)
    locations = list(
        StorageLocation.objects.select_for_update()
        .filter(pk__in=[location.pk for location in locations])
        .order_by("pk")
    )
    expected_count = 1 if doc.is_cell_recount else len(canonical_cell_codes(doc.section_code))
    if len(locations) != expected_count:
        raise SectionRecountError("Не удалось захватить все ячейки пересчёта.")
    if (
        StockLocationLock.objects.filter(
            location_id__in=[location.pk for location in locations], released_at__isnull=True
        )
        .exclude(document_id=doc.pk)
        .exists()
    ):
        raise SectionRecountError("Участок уже заблокирован другой складской операцией.")
    codes = [doc.section_code] if doc.is_cell_recount else canonical_cell_codes(doc.section_code)
    if [location.code for location in sorted(locations, key=lambda item: item.code)] != codes:
        raise SectionRecountError("Структура ячеек изменилась до захвата блокировки.")
    if any(not location.can_hold_stock() for location in locations):
        raise SectionRecountError("Одна из ячеек участка недоступна для хранения.")
    if doc.is_cell_recount and StockLot.objects.filter(
        location_id__in=[location.pk for location in locations],
        status=StockLot.Status.RECEIVING,
        quantity__gt=0,
    ).exists():
        raise SectionRecountError(
            "В ячейке есть незавершённая приёмка. Сначала завершите или отмените её."
        )
    if PartItem.objects.filter(
        current_location_id__in=[location.pk for location in locations],
        status__in=ITEM_PHYSICAL_STATUSES,
    ).exists():
        raise SectionRecountError(
            "В ячейке есть поштучные экземпляры. Для них нужен пересчёт с идентификацией "
            "каждого экземпляра; количественный пересчёт не запущен."
        )
    ordered_locations = sorted(locations, key=lambda item: item.code)
    snapshot = _capture_snapshot(
        ordered_locations, section_code=doc.section_code, scope=doc.scope
    )
    doc.status = SectionRecount.Status.COUNTING
    doc.started_at = timezone.now()
    doc.snapshot = snapshot
    doc.snapshot_fingerprint = snapshot["fingerprint"]
    doc.save(
        update_fields=["status", "started_at", "snapshot", "snapshot_fingerprint", "updated_at"]
    )
    if create_cells:
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


def _preferred_snapshot_for_part(part_id: int) -> dict:
    preferred = (
        PartPreferredLocation.objects.filter(part_type_id=part_id)
        .values("location_id", "updated_at")
        .first()
    )
    if preferred is None:
        return {}
    return {
        "location_id": preferred["location_id"],
        "updated_at": preferred["updated_at"].isoformat(),
    }


@transaction.atomic
def record_section_scan(doc: SectionRecount, *, cell_number: int, raw_value: str, by=None):
    doc = SectionRecount.objects.select_for_update().get(pk=doc.pk)
    _ensure_counting(doc)
    _reopen_ready(doc)
    cell = _get_cell(doc, cell_number)
    part, part_number = _resolve_exact_part(raw_value)
    return _record_part(doc, cell, part, part_number)


def _record_part(doc, cell, part, part_number):
    line = (
        SectionRecountLine.objects.select_for_update()
        .filter(recount=doc, cell=cell, part_type=part)
        .first()
    )
    if line is None:
        line = SectionRecountLine.objects.create(
            recount=doc,
            cell=cell,
            part_type=part,
            part_number=part_number,
            preferred_snapshot=_preferred_snapshot_for_part(part.pk),
            quantity=1,
        )
    else:
        line.quantity += Decimal("1")
        line.save(update_fields=["quantity", "updated_at"])
    if cell.status == SectionRecountCell.Status.COMPLETED:
        cell.status = SectionRecountCell.Status.COUNTING
        cell.save(update_fields=["status"])
    return line


@transaction.atomic
def record_section_part(doc: SectionRecount, *, cell_number: int, part_id: int, by=None):
    """Добавить выбранный read-only поиском PartType без подмены его identity."""
    doc = SectionRecount.objects.select_for_update().get(pk=doc.pk)
    _ensure_counting(doc)
    _reopen_ready(doc)
    cell = _get_cell(doc, cell_number)
    try:
        part = PartType.objects.get(pk=part_id)
    except PartType.DoesNotExist as exc:
        raise SectionRecountError("Выбранная деталь больше не существует.") from exc
    return _record_part(doc, cell, part, _part_number(part))


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
    batch_line_ids = list(line.allocations.values_list("batch_line_id", flat=True))
    # Deletion frees capacity in the same global bucket as allocation create or
    # update, so it must participate in the same BatchLine lock protocol.
    list(
        BatchLine.objects.select_for_update()
        .filter(pk__in=batch_line_ids)
        .order_by("pk")
    )
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


def _allocation_statuses(doc: SectionRecount, batch_line_id: int) -> list[str]:
    statuses = _source_statuses(doc, batch_line_id)
    if statuses or not doc.is_cell_recount:
        return statuses
    return list(PHYSICAL_LOT_STATUSES)


def _cell_batch_capacity(doc: SectionRecount, batch_line: BatchLine) -> Decimal:
    """Максимальный факт по партии в одной ячейке без превышения закупки."""
    source_total = sum(
        (
            quantity
            for (batch_line_id, _status), quantity in _snapshot_source_quantities(doc).items()
            if batch_line_id == batch_line.pk
        ),
        Decimal("0"),
    )
    global_physical = (
        StockLot.objects.filter(
            batch_line=batch_line,
            status__in=PHYSICAL_LOT_STATUSES,
            quantity__gt=0,
        ).aggregate(total=Sum("quantity"))["total"]
        or Decimal("0")
    )
    recoverable = max(batch_line.quantity - global_physical, Decimal("0"))
    return source_total + recoverable


def _allocation_cost(
    doc: SectionRecount, batch_line: BatchLine, lot_status: str
) -> Decimal | None:
    snapshot_cost = _snapshot_source_costs(doc).get((batch_line.pk, lot_status))
    if snapshot_cost is not None:
        return snapshot_cost
    if doc.is_cell_recount and batch_line.batch.cost_finalized:
        return batch_line.landed_unit_cost_rub
    return None


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
    # BatchLine is the stable row shared by every allocation competing for the
    # same global source quantity. Lock it before reading the aggregate so a
    # second recount line waits and then observes the committed allocation.
    batch_line = (
        BatchLine.objects.select_for_update()
        .select_related("part_type", "batch")
        .get(pk=batch_line_id)
    )
    if batch_line.part_type_id != line.part_type_id:
        raise SectionRecountError("Партия не относится к этой детали.")
    if batch_line.batch.status not in VALID_BATCH_STATUSES or not batch_line.batch.cost_finalized:
        raise SectionRecountError("Партия не разрешена для складского остатка.")
    if lot_status not in PHYSICAL_LOT_STATUSES:
        raise SectionRecountError("Недопустимый статус лота для пересчёта.")
    source_quantity = _snapshot_source_quantities(line.recount).get(
        (batch_line.pk, lot_status), Decimal("0")
    )
    source_cost = _allocation_cost(line.recount, batch_line, lot_status)
    if source_cost is None:
        raise SectionRecountError("Для выбранной партии нет подтверждённой себестоимости.")
    existing = SectionRecountAllocation.objects.filter(
        line__recount=line.recount, batch_line=batch_line
    ).exclude(line=line)
    allocated_elsewhere = existing.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    capacity = (
        _cell_batch_capacity(line.recount, batch_line)
        if line.recount.is_cell_recount
        else source_quantity
    )
    if capacity <= 0:
        raise SectionRecountError(
            "Для выбранной партии нет допустимого остатка или свободного лимита."
        )
    if allocated_elsewhere + quantity > capacity:
        limit_label = (
            "допустимый лимит партии"
            if line.recount.is_cell_recount
            else "snapshot-остаток партии/статуса"
        )
        raise SectionRecountError(
            f"Распределение {allocated_elsewhere + quantity} превышает "
            f"{limit_label} {capacity}."
        )
    allocation, _ = SectionRecountAllocation.objects.update_or_create(
        line=line,
        batch_line=batch_line,
        defaults={
            "quantity": quantity,
            "unit_cost_rub": source_cost,
            "batch_quantity_snapshot": batch_line.quantity,
            "batch_updated_at_snapshot": batch_line.updated_at,
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
            candidate = (
                BatchLine.objects.select_for_update()
                .select_related("part_type", "batch")
                .get(pk=candidates[0].pk)
            )
            statuses = _source_statuses(doc, candidate.pk)
            if len(statuses) != 1:
                continue
            status = statuses[0]
            source_quantity = (
                _cell_batch_capacity(doc, candidate)
                if doc.is_cell_recount
                else _snapshot_source_quantities(doc).get(
                    (candidate.pk, status), Decimal("0")
                )
            )
            allocated = (
                SectionRecountAllocation.objects.filter(
                    line__recount=doc, batch_line=candidate, lot_status=status
                ).aggregate(total=Sum("quantity"))["total"]
                or Decimal("0")
            )
            if allocated + line.quantity > source_quantity:
                continue
            SectionRecountAllocation.objects.create(
                line=line,
                batch_line=candidate,
                quantity=line.quantity,
                unit_cost_rub=_allocation_cost(doc, candidate, status),
                batch_quantity_snapshot=candidate.quantity,
                batch_updated_at_snapshot=candidate.updated_at,
                lot_status=status,
            )


def _validate_allocations(doc: SectionRecount) -> list[str]:
    warnings = []
    source_quantities = _snapshot_source_quantities(doc)
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
            total_key = allocation.batch_line_id if doc.is_cell_recount else key
            totals[total_key] += allocation.quantity
            if allocation.batch_line.part_type_id != line.part_type_id:
                warnings.append(f"{line.part_number}: партия относится к другой детали")
            if allocation.lot_status not in PHYSICAL_LOT_STATUSES:
                warnings.append(f"{line.part_number}: недопустимый статус лота")
            if (
                allocation.batch_line.batch.status not in VALID_BATCH_STATUSES
                or not allocation.batch_line.batch.cost_finalized
            ):
                warnings.append(f"{line.part_number}: партия недоступна для склада")
            expected_cost = _allocation_cost(
                doc, allocation.batch_line, allocation.lot_status
            )
            if allocation.unit_cost_rub != expected_cost:
                warnings.append(f"{line.part_number}: себестоимость не совпадает с snapshot")
            if (
                allocation.batch_quantity_snapshot != allocation.batch_line.quantity
                or allocation.batch_updated_at_snapshot != allocation.batch_line.updated_at
            ):
                warnings.append(f"{line.part_number}: метаданные партии изменились")
            if key not in source_quantities and not doc.is_cell_recount:
                warnings.append(f"{line.part_number}: исходный лот отсутствует в snapshot")
            capacity = (
                _cell_batch_capacity(doc, allocation.batch_line)
                if doc.is_cell_recount
                else source_quantities.get(key, Decimal("0"))
            )
            if totals[total_key] > capacity:
                warnings.append(
                    f"партия {allocation.batch_line_id} / {allocation.lot_status}: "
                    "распределение превышает допустимый лимит"
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


def _adjustment_plan(doc: SectionRecount) -> dict[str, list[dict]]:
    """Сравнить lot-level snapshot с целевыми allocation и вернуть только дельты."""
    source = defaultdict(Decimal)
    source_lot_ids = {}
    for item in doc.snapshot.get("lots", []):
        if item["status"] not in PHYSICAL_LOT_STATUSES:
            continue
        key = (item["location_id"], item["batch_line_id"], item["status"])
        source[key] += _decimal(item["quantity"])
        source_lot_ids[key] = item["id"]

    target = defaultdict(Decimal)
    target_meta = {}
    allocations = SectionRecountAllocation.objects.filter(
        line__recount=doc, quantity__gt=0
    ).select_related("line__cell", "batch_line")
    for allocation in allocations:
        key = (
            allocation.line.cell.location_id,
            allocation.batch_line_id,
            allocation.lot_status,
        )
        target[key] += allocation.quantity
        target_meta[key] = {
            "batch_line_id": allocation.batch_line_id,
            "location_id": allocation.line.cell.location_id,
            "lot_status": allocation.lot_status,
        }

    adjust_out = []
    adjust_in = []
    for key in sorted(set(source) | set(target)):
        delta = target[key] - source[key]
        if delta < 0:
            adjust_out.append(
                {
                    "lot_id": source_lot_ids[key],
                    "quantity": -delta,
                    "location_id": key[0],
                    "batch_line_id": key[1],
                    "lot_status": key[2],
                }
            )
        elif delta > 0:
            adjust_in.append({**target_meta[key], "quantity": delta})
    return {"adjust_out": adjust_out, "adjust_in": adjust_in}


def _comparison_status(before: Decimal, after: Decimal) -> tuple[str, str]:
    if before == after:
        return "match", "Совпадает"
    if before > 0 and after == 0:
        return "system_only", "В системе есть, физически не найдено"
    if before == 0 and after > 0:
        return "actual_only", "Физически найдено, в системе не числится"
    if before > after:
        return "system_more", "В системе больше"
    return "actual_more", "Фактически больше"


def build_section_dry_run(doc: SectionRecount) -> dict:
    """Построить отчёт без записи в БД, включая все расхождения."""
    current = _snapshot_totals(doc)
    counted = defaultdict(lambda: defaultdict(Decimal))
    for line in doc.lines.select_related("cell").all():
        counted[line.cell.location_id][line.part_type_id] += line.quantity
    cell_rows = []
    comparison_rows = []
    all_keys = set(current) | set(counted)
    part_ids = {
        part_id
        for location_id in all_keys
        for part_id in set(current[location_id]) | set(counted[location_id])
    }
    parts = {
        part.pk: part
        for part in with_part_identity(
            PartType.objects.filter(pk__in=part_ids), part_field=""
        )
    }
    locations = {
        item["id"]: item["code"] for item in doc.snapshot.get("locations", [])
    }
    for location_id in sorted(all_keys):
        part_ids = set(current[location_id]) | set(counted[location_id])
        changes = []
        for part_id in sorted(part_ids):
            before = current[location_id][part_id]
            after = counted[location_id][part_id]
            status, status_label = _comparison_status(before, after)
            part = parts[part_id]
            comparison = {
                "location_id": location_id,
                "location_code": locations.get(location_id, ""),
                "part_type_id": part_id,
                "part_number": _part_number(part),
                "part_name": part.name,
                "before": str(before),
                "after": str(after),
                "difference": str(after - before),
                "status": status,
                "status_label": status_label,
            }
            comparison_rows.append(comparison)
            if before != after:
                changes.append(comparison)
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
            "batch_number": allocation.batch_line.batch.number,
            "status": allocation.lot_status,
            "quantity": str(allocation.quantity),
            "unit_cost_rub": str(allocation.unit_cost_rub),
            "batch_quantity_snapshot": str(allocation.batch_quantity_snapshot),
        }
        for allocation in SectionRecountAllocation.objects.filter(
            line__recount=doc
        ).select_related("line__cell__location", "batch_line__batch")
    ]
    adjustment_plan = _adjustment_plan(doc)
    adjust_out_quantity = sum(
        (item["quantity"] for item in adjustment_plan["adjust_out"]), Decimal("0")
    )
    adjust_in_quantity = sum(
        (item["quantity"] for item in adjustment_plan["adjust_in"]), Decimal("0")
    )
    return {
        "section_code": doc.section_code,
        "before_total": str(before_total),
        "after_total": str(after_total),
        "difference": str(after_total - before_total),
        "cell_rows": cell_rows,
        "comparison_rows": comparison_rows,
        "moved": moved,
        "adjust_out": str(adjust_out_quantity),
        "adjust_in": str(adjust_in_quantity),
        "adjust_out_movements": len(adjustment_plan["adjust_out"]),
        "adjust_in_movements": len(adjustment_plan["adjust_in"]),
        "matched_positions": sum(row["status"] == "match" for row in comparison_rows),
        "shortage_positions": sum(
            _decimal(row["difference"]) < 0 for row in comparison_rows
        ),
        "excess_positions": sum(
            _decimal(row["difference"]) > 0 for row in comparison_rows
        ),
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
        if doc.is_cell_recount:
            raise SectionRecountError("Сначала завершите физический пересчёт ячейки.")
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
    live = _capture_snapshot(
        locations, section_code=doc.section_code, scope=doc.scope
    )
    for line in doc.lines.all():
        if _preferred_snapshot_for_part(line.part_type_id) != line.preferred_snapshot:
            raise SectionRecountError(
                f"Snapshot mismatch: изменилась предпочтительная ячейка "
                f"детали {line.part_number}."
            )
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
    part_ids = {
        item["part_type_id"] for item in doc.snapshot.get("lots", [])
    } | set(doc.lines.values_list("part_type_id", flat=True))
    locations_by_part = defaultdict(set)
    for lot in StockLot.objects.filter(
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
            current_preferred = PartPreferredLocation.objects.filter(
                part_type_id=part_id
            ).values_list("location_id", flat=True).first()
            if current_preferred != location_id:
                set_preferred_part_location(
                    PartType.objects.get(pk=part_id),
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
    batch_line_ids = list(
        SectionRecountAllocation.objects.filter(line__recount=doc)
        .values_list("batch_line_id", flat=True)
        .distinct()
    )
    list(
        BatchLine.objects.select_for_update()
        .filter(pk__in=batch_line_ids)
        .order_by("pk")
    )
    list(
        StockLot.objects.select_for_update()
        .filter(batch_line_id__in=batch_line_ids)
        .order_by("pk")
    )
    _assert_snapshot_unchanged(doc)
    warnings = _validate_allocations(doc)
    if warnings:
        raise SectionRecountError(
            "Проверка распределения партий не пройдена: " + "; ".join(warnings[:3])
        )
    doc.status = SectionRecount.Status.APPLYING
    doc.save(update_fields=["status", "updated_at"])
    adjustment_plan = _adjustment_plan(doc)
    movement_count = 0
    for adjustment in adjustment_plan["adjust_out"]:
        lot = StockLot.objects.get(pk=adjustment["lot_id"])
        adjust_stock_lot_quantity(
            lot,
            -adjustment["quantity"],
            by=doc.created_by,
            comment=f"{doc.operation_label} #{doc.pk}",
            document_type=SECTION_RECOUNT_DOCUMENT, document_id=doc.pk,
            section_recount_id=doc.pk, update_preferred=False,
        )
        movement_count += 1
    batch_lines = {
        line.pk: line
        for line in BatchLine.objects.filter(pk__in=batch_line_ids).select_related(
            "batch", "part_type"
        )
    }
    locations = {
        location.pk: location
        for location in StorageLocation.objects.filter(
            pk__in=doc.cells.values("location_id")
        )
    }
    for adjustment in adjustment_plan["adjust_in"]:
        lot = get_or_create_section_recount_lot(
            batch_lines[adjustment["batch_line_id"]],
            locations[adjustment["location_id"]],
            lot_status=adjustment["lot_status"],
            section_recount_id=doc.pk,
        )
        adjust_stock_lot_quantity(
            lot,
            adjustment["quantity"],
            by=doc.created_by,
            comment=f"{doc.operation_label} #{doc.pk}",
            document_type=SECTION_RECOUNT_DOCUMENT,
            document_id=doc.pk,
            section_recount_id=doc.pk,
            update_preferred=False,
        )
        movement_count += 1
    result = dict(doc.result or build_section_dry_run(doc))
    result["preferred"] = _reconcile_preferred(doc)
    result["created_movements"] = movement_count
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
