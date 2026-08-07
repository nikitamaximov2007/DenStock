"""Безопасный workflow полного пересчёта фиксированного участка."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.db.models import Sum
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import (
    PartPreferredLocation,
    StockBalance,
    StockLocationLock,
    StockLot,
    StockMovement,
)
from apps.inventory.services import (
    InventoryError,
    create_stock_lot,
    move_stock_lot,
    receive_stock_lot,
    set_preferred_part_location,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Reservation, ReservationLine
from apps.stocktaking.models import (
    SectionRecount,
    SectionRecountAllocation,
    SectionRecountLine,
)
from apps.stocktaking.section_recount import (
    SectionRecountError,
    allocate_section_line,
    apply_section_recount,
    build_section_dry_run,
    cancel_section_recount,
    complete_section_cell,
    create_section_recount,
    mark_section_ready,
    record_section_scan,
    set_section_line_quantity,
    start_section_recount,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


def _target_locations(without_c05=True):
    codes = [f"S03-L03-D02-C{number:02d}" for number in range(1, 11)]
    if without_c05:
        codes.remove("S03-L03-D02-C05")
    return [
        StorageLocation.objects.create(code=code, name=code, storage_allowed=True, is_active=True)
        for code in codes
    ]


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="section-admin", password="secret")


@pytest.fixture
def section_data(db, admin):
    _target_locations()
    supplier = Supplier.objects.create(name="Поставщик пересчёта")
    category = Category.objects.create(name="Категория пересчёта")
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    part = PartType.objects.create(
        name="Деталь пересчёта",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=part, value="RC-0001", kind=PartNumber.Kind.OEM, is_primary=True)
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("10"))
    batch_line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal("10"),
        unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    batch_line.refresh_from_db()
    source = StorageLocation.objects.get(code="S03-L03-D02-C01")
    lot = create_stock_lot(batch_line, source, Decimal("5"))
    receive_stock_lot(lot, by=admin)
    return {"admin": admin, "part": part, "batch_line": batch_line, "lot": lot}


def _start(data):
    doc = create_section_recount(by=data["admin"])
    start_section_recount(doc)
    doc.refresh_from_db()
    return doc


def _complete_all_cells(doc):
    for number in range(1, 11):
        complete_section_cell(doc, cell_number=number)


def _ready(data):
    doc = _start(data)
    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=data["admin"])
    _complete_all_cells(doc)
    return mark_section_ready(doc)


def _active_reservation(data, quantity="1"):
    reservation = Reservation.objects.create(
        customer_name="Клиент пересчёта", status=Reservation.Status.ACTIVE
    )
    ReservationLine.objects.create(
        reservation=reservation,
        part_type=data["part"],
        stock_lot=data["lot"],
        quantity=Decimal(quantity),
    )
    return reservation


def test_start_creates_only_missing_c05_and_durable_locks(section_data):
    doc = _start(section_data)

    assert doc.status == SectionRecount.Status.COUNTING
    assert list(doc.cells.values_list("location__code", flat=True)) == [
        f"S03-L03-D02-C{number:02d}" for number in range(1, 11)
    ]
    assert StorageLocation.objects.filter(code__startswith="S03-L03-D02-C").count() == 10
    assert StockLocationLock.objects.filter(
        document_id=doc.pk, released_at__isnull=True
    ).count() == 10


def test_scan_and_dry_run_do_not_change_stock(section_data):
    data = section_data
    doc = _start(data)
    before = {
        "lots": list(StockLot.objects.values_list("pk", "quantity", "status")),
        "balances": list(StockBalance.objects.values_list("pk", "quantity_physical")),
        "movements": StockMovement.objects.count(),
    }

    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=data["admin"])
    _complete_all_cells(doc)
    report = build_section_dry_run(doc)

    assert report["before_total"] == "5.000"
    assert report["after_total"] == "1.000"
    assert list(StockLot.objects.values_list("pk", "quantity", "status")) == before["lots"]
    assert list(StockBalance.objects.values_list("pk", "quantity_physical")) == before["balances"]
    assert StockMovement.objects.count() == before["movements"]


def test_apply_is_atomic_linked_and_idempotent(section_data):
    data = section_data
    doc = _start(data)
    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=data["admin"])
    _complete_all_cells(doc)
    doc = mark_section_ready(doc)

    completed = apply_section_recount(doc)
    completed.refresh_from_db()
    assert completed.status == SectionRecount.Status.COMPLETED
    assert StockLot.objects.get(pk=data["lot"].pk).quantity == Decimal("0.000")
    moved_lot = StockLot.objects.get(
        batch_line=data["batch_line"], location__code="S03-L03-D02-C02"
    )
    assert moved_lot.quantity == Decimal("1.000")
    movements = StockMovement.objects.filter(document_type="section_recount", document_id=doc.pk)
    assert movements.count() == 2
    assert completed.result["created_movements"] == 2

    assert apply_section_recount(completed).pk == completed.pk
    assert StockMovement.objects.filter(
        document_type="section_recount", document_id=doc.pk
    ).count() == 2


def test_multi_batch_fact_requires_explicit_allocation(section_data):
    data = section_data
    second_batch = Batch.objects.create(supplier=Supplier.objects.first())
    second_line = BatchLine.objects.create(
        batch=second_batch,
        part_type=data["part"],
        quantity=Decimal("10"),
        unit_cost_currency=Decimal("120"),
    )
    second_batch.status = Batch.Status.ACCEPTED
    second_batch.save(update_fields=["status"])
    finalize_cost(second_batch, data["admin"])
    second_line.refresh_from_db()

    doc = _start(data)
    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=data["admin"])
    _complete_all_cells(doc)
    with pytest.raises(SectionRecountError, match="партии"):
        mark_section_ready(doc)


def test_editing_ready_document_invalidates_old_dry_run(section_data):
    data = section_data
    doc = _start(data)
    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=data["admin"])
    _complete_all_cells(doc)
    doc = mark_section_ready(doc)
    line = doc.lines.get()
    set_section_line_quantity(line, "2")
    doc.refresh_from_db()
    assert doc.status == SectionRecount.Status.COUNTING
    assert doc.result == {}


def test_locked_section_rejects_new_lot_without_changing_stock(section_data):
    data = section_data
    doc = _start(data)
    target = StorageLocation.objects.get(code="S03-L03-D02-C02")
    with pytest.raises(InventoryError, match="заблокирована"):
        create_stock_lot(data["batch_line"], target, Decimal("1"))
    assert not StockLot.objects.filter(batch_line=data["batch_line"], location=target).exists()
    assert doc.status == SectionRecount.Status.COUNTING


def test_locked_section_rejects_preferred_location_update(section_data):
    data = section_data
    _start(data)
    target = StorageLocation.objects.get(code="S03-L03-D02-C02")
    before = PartPreferredLocation.objects.get(part_type=data["part"])
    with pytest.raises(InventoryError, match="заблокирована"):
        set_preferred_part_location(data["part"], target, by=data["admin"])
    after = PartPreferredLocation.objects.get(part_type=data["part"])
    assert after.location_id == before.location_id


def test_snapshot_change_fails_without_partial_apply(section_data):
    data = section_data
    doc = _start(data)
    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=data["admin"])
    _complete_all_cells(doc)
    doc = mark_section_ready(doc)
    StockLot.objects.filter(pk=data["lot"].pk).update(quantity=Decimal("4"))

    with pytest.raises(SectionRecountError, match="Snapshot"):
        apply_section_recount(doc)
    doc.refresh_from_db()
    assert doc.status == SectionRecount.Status.FAILED
    assert StockMovement.objects.filter(
        document_type="section_recount", document_id=doc.pk
    ).count() == 0
    assert StockLot.objects.get(pk=data["lot"].pk).quantity == Decimal("4")


def test_reserve_before_snapshot_is_shown_and_blocks_ready(section_data):
    data = section_data
    _active_reservation(data, "2")
    doc = _start(data)
    assert doc.snapshot["reservations"][0]["quantity"] == "2.000"
    _complete_all_cells(doc)
    with pytest.raises(SectionRecountError, match="активные резервы"):
        mark_section_ready(doc)
    assert StockMovement.objects.filter(document_type="section_recount").count() == 0


def test_reserve_added_after_snapshot_blocks_apply_without_movements(section_data):
    data = section_data
    doc = _ready(data)
    _active_reservation(data, "1")
    with pytest.raises(SectionRecountError, match="Snapshot mismatch"):
        apply_section_recount(doc)
    doc.refresh_from_db()
    assert doc.status == SectionRecount.Status.FAILED
    assert not StockMovement.objects.filter(document_type="section_recount").exists()


def test_reserve_changed_after_snapshot_blocks_apply_without_movements(section_data):
    data = section_data
    doc = _ready(data)
    reservation = _active_reservation(data, "1")
    ReservationLine.objects.filter(reservation=reservation).update(quantity=Decimal("2"))
    with pytest.raises(SectionRecountError, match="Snapshot mismatch"):
        apply_section_recount(doc)
    assert not StockMovement.objects.filter(document_type="section_recount").exists()


@pytest.mark.parametrize("fail_after", [1, 2])
def test_apply_fault_after_adjust_rolls_back_everything(section_data, fail_after):
    data = section_data
    doc = _ready(data)
    before_lots = list(StockLot.objects.values_list("pk", "quantity", "status"))
    before_balances = list(
        StockBalance.objects.values_list("pk", "quantity_physical", "quantity_available")
    )
    before_movements = StockMovement.objects.count()
    calls = 0

    from apps.inventory.services import adjust_stock_lot_quantity as original_adjust

    def fail_after_adjust(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_adjust(*args, **kwargs)
        if calls == fail_after:
            raise RuntimeError("fault injection")
        return result

    with patch(
        "apps.stocktaking.section_recount.adjust_stock_lot_quantity",
        side_effect=fail_after_adjust,
    ):
        with pytest.raises(SectionRecountError):
            apply_section_recount(doc)
    doc.refresh_from_db()
    assert doc.status == SectionRecount.Status.FAILED
    assert list(StockLot.objects.values_list("pk", "quantity", "status")) == before_lots
    assert list(
        StockBalance.objects.values_list("pk", "quantity_physical", "quantity_available")
    ) == before_balances
    assert StockMovement.objects.count() == before_movements


def test_apply_fault_during_preferred_update_rolls_back_everything(section_data):
    data = section_data
    doc = _ready(data)
    before_lots = list(StockLot.objects.values_list("pk", "quantity", "status"))
    before_preferences = list(
        PartPreferredLocation.objects.values_list("part_type_id", "location_id")
    )
    with patch(
        "apps.stocktaking.section_recount.set_preferred_part_location",
        side_effect=RuntimeError("preferred fault"),
    ):
        with pytest.raises(SectionRecountError):
            apply_section_recount(doc)
    doc.refresh_from_db()
    assert doc.status == SectionRecount.Status.FAILED
    assert list(StockLot.objects.values_list("pk", "quantity", "status")) == before_lots
    assert list(
        PartPreferredLocation.objects.values_list("part_type_id", "location_id")
    ) == before_preferences


def test_location_and_preferred_changes_after_snapshot_block_apply(section_data):
    data = section_data
    doc = _ready(data)
    location = StorageLocation.objects.get(code="S03-L03-D02-C01")
    location.name = "Переименовано после snapshot"
    location.save(update_fields=["name"])
    with pytest.raises(SectionRecountError, match="ячейк"):
        apply_section_recount(doc)
    doc.refresh_from_db()
    assert doc.status == SectionRecount.Status.FAILED
    cancel_section_recount(doc)

    doc = _ready(data)
    preference = PartPreferredLocation.objects.get(part_type=data["part"])
    preference.location = StorageLocation.objects.get(code="S03-L03-D02-C02")
    preference.save(update_fields=["location", "updated_at"])
    with pytest.raises(SectionRecountError, match="предпочтительная"):
        apply_section_recount(doc)


def test_quarantine_status_is_preserved_in_target(section_data):
    data = section_data
    data["lot"].status = StockLot.Status.QUARANTINE
    data["lot"].save(update_fields=["status"])
    doc = _ready(data)
    assert doc.result["allocations"][0]["status"] == StockLot.Status.QUARANTINE
    completed = apply_section_recount(doc)
    target = StockLot.objects.get(batch_line=data["batch_line"], location__code="S03-L03-D02-C02")
    assert completed.status == SectionRecount.Status.COMPLETED
    assert target.status == StockLot.Status.QUARANTINE


def test_allocation_global_limit_and_invalid_batch_are_rejected(section_data):
    data = section_data
    doc = _start(data)
    first = record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=data["admin"])
    second = record_section_scan(doc, cell_number=3, raw_value="RC-0001", by=data["admin"])
    with pytest.raises(SectionRecountError, match="snapshot-остаток"):
        allocate_section_line(
            first, batch_line_id=data["batch_line"].pk, quantity="4", lot_status="available"
        )
        allocate_section_line(
            second, batch_line_id=data["batch_line"].pk, quantity="2", lot_status="available"
        )

    data["batch_line"].batch.status = Batch.Status.CANCELED
    data["batch_line"].batch.save(update_fields=["status"])
    with pytest.raises(SectionRecountError, match="разрешена"):
        allocate_section_line(
            first, batch_line_id=data["batch_line"].pk, quantity="1", lot_status="available"
        )


def _allocation_lines(data, quantities):
    doc = _start(data)
    lines = []
    for cell_number, quantity in zip((2, 3), quantities, strict=True):
        line = record_section_scan(
            doc, cell_number=cell_number, raw_value="RC-0001", by=data["admin"]
        )
        lines.append(set_section_line_quantity(line, quantity))
    return doc, lines


def _add_available_source(data):
    lot = create_stock_lot(
        data["batch_line"],
        StorageLocation.objects.get(code="S03-L03-D02-C04"),
        Decimal("5"),
    )
    return receive_stock_lot(lot, by=data["admin"])


def _allocate_concurrently(barrier, line_id, batch_line_id, quantity, lot_status):
    close_old_connections()
    try:
        assert barrier.wait(10)
        return allocate_section_line(
            SectionRecountLine.objects.get(pk=line_id),
            batch_line_id=batch_line_id,
            quantity=quantity,
            lot_status=lot_status,
        )
    except Exception as exc:  # noqa: BLE001 - worker result is asserted by the test.
        return exc
    finally:
        close_old_connections()


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL allocation lock integration test"
)
def test_postgresql_allocation_limit_serializes_three_plus_three(section_data):
    data = section_data
    doc, lines = _allocation_lines(data, ("3", "3"))
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _allocate_concurrently,
                barrier,
                line.pk,
                data["batch_line"].pk,
                "3",
                StockLot.Status.AVAILABLE,
            )
            for line in lines
        ]
        results = [future.result() for future in futures]
    assert sum(isinstance(result, SectionRecountAllocation) for result in results) == 1
    assert sum(isinstance(result, SectionRecountError) for result in results) == 1
    total = SectionRecountAllocation.objects.filter(
        line__recount=doc,
        batch_line=data["batch_line"],
        lot_status=StockLot.Status.AVAILABLE,
    ).aggregate(total=Sum("quantity"))["total"]
    assert total == Decimal("3")
    assert total <= Decimal("5")


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL allocation lock integration test"
)
def test_postgresql_allocation_limit_serializes_four_plus_six(section_data):
    data = section_data
    _add_available_source(data)
    doc, lines = _allocation_lines(data, ("4", "6"))
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _allocate_concurrently,
                barrier,
                line.pk,
                data["batch_line"].pk,
                quantity,
                StockLot.Status.AVAILABLE,
            )
            for line, quantity in zip(lines, ("4", "6"), strict=True)
        ]
        results = [future.result() for future in futures]
    assert all(isinstance(result, SectionRecountAllocation) for result in results)
    total = SectionRecountAllocation.objects.filter(
        line__recount=doc,
        batch_line=data["batch_line"],
        lot_status=StockLot.Status.AVAILABLE,
    ).aggregate(total=Sum("quantity"))["total"]
    assert total == Decimal("10")


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL allocation lock integration test"
)
def test_postgresql_allocation_limit_rejects_six_plus_six(section_data):
    data = section_data
    _add_available_source(data)
    doc, lines = _allocation_lines(data, ("6", "6"))
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _allocate_concurrently,
                barrier,
                line.pk,
                data["batch_line"].pk,
                "6",
                StockLot.Status.AVAILABLE,
            )
            for line in lines
        ]
        results = [future.result() for future in futures]
    assert sum(isinstance(result, SectionRecountAllocation) for result in results) == 1
    assert sum(isinstance(result, SectionRecountError) for result in results) == 1
    total = SectionRecountAllocation.objects.filter(
        line__recount=doc,
        batch_line=data["batch_line"],
        lot_status=StockLot.Status.AVAILABLE,
    ).aggregate(total=Sum("quantity"))["total"]
    assert total == Decimal("6")
    assert total <= Decimal("10")


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL allocation lock integration test"
)
def test_postgresql_allocation_limit_is_separate_by_lot_status(section_data):
    data = section_data
    second_lot = create_stock_lot(
        data["batch_line"],
        StorageLocation.objects.get(code="S03-L03-D02-C04"),
        Decimal("5"),
    )
    receive_stock_lot(second_lot, by=data["admin"])
    StockLot.objects.filter(pk=second_lot.pk).update(status=StockLot.Status.QUARANTINE)
    doc, lines = _allocation_lines(data, ("5", "5"))
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _allocate_concurrently,
                barrier,
                lines[0].pk,
                data["batch_line"].pk,
                "5",
                StockLot.Status.AVAILABLE,
            ),
            pool.submit(
                _allocate_concurrently,
                barrier,
                lines[1].pk,
                data["batch_line"].pk,
                "5",
                StockLot.Status.QUARANTINE,
            ),
        ]
        results = [future.result() for future in futures]
    assert all(isinstance(result, SectionRecountAllocation) for result in results)
    totals = dict(
        SectionRecountAllocation.objects.filter(line__recount=doc)
        .values("lot_status")
        .annotate(total=Sum("quantity"))
        .values_list("lot_status", "total")
    )
    assert totals == {
        StockLot.Status.AVAILABLE: Decimal("5"),
        StockLot.Status.QUARANTINE: Decimal("5"),
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL allocation lock integration test"
)
def test_postgresql_allocation_limit_serializes_concurrent_updates(section_data):
    data = section_data
    _add_available_source(data)
    doc, lines = _allocation_lines(data, ("4", "4"))
    for line in lines:
        allocate_section_line(
            line,
            batch_line_id=data["batch_line"].pk,
            quantity="4",
            lot_status=StockLot.Status.AVAILABLE,
        )
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _allocate_concurrently,
                barrier,
                line.pk,
                data["batch_line"].pk,
                "7",
                StockLot.Status.AVAILABLE,
            )
            for line in lines
        ]
        results = [future.result() for future in futures]
    assert sum(isinstance(result, SectionRecountAllocation) for result in results) == 1
    assert sum(isinstance(result, SectionRecountError) for result in results) == 1
    total = SectionRecountAllocation.objects.filter(
        line__recount=doc,
        batch_line=data["batch_line"],
        lot_status=StockLot.Status.AVAILABLE,
    ).aggregate(total=Sum("quantity"))["total"]
    assert total <= Decimal("10")


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL row-lock integration test"
)
def test_postgresql_row_lock_integration_marker():
    """Executed in the PostgreSQL test environment, skipped on local SQLite."""
    assert connection.vendor == "postgresql"


def _parallel_call(function, *args):
    close_old_connections()
    try:
        return function(*args)
    finally:
        close_old_connections()


def _parallel_call_captured(function, *args):
    try:
        return _parallel_call(function, *args)
    except Exception as exc:  # noqa: BLE001 - the assertion checks the exact outcome.
        return exc


def _create_and_start_concurrently(admin, barrier):
    close_old_connections()
    try:
        barrier.wait(10)
        doc = create_section_recount(by=admin)
        return start_section_recount(doc)
    except Exception as exc:  # noqa: BLE001 - the assertion checks the exact outcome.
        return exc
    finally:
        close_old_connections()


def _start_with_snapshot_gate(doc, entered, release):
    import apps.stocktaking.section_recount as recount_module

    original_capture = recount_module._capture_snapshot

    def gated_capture(*args, **kwargs):
        entered.set()
        assert release.wait(10), "start transaction did not receive the release signal"
        return original_capture(*args, **kwargs)

    with patch.object(recount_module, "_capture_snapshot", gated_capture):
        return _parallel_call(start_section_recount, doc)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL row-lock integration test"
)
def test_postgresql_concurrent_start_has_one_owner(section_data):
    data = section_data
    # The active-document constraint prevents two pre-created drafts; race the
    # real create-and-start workflow instead.
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _create_and_start_concurrently(data["admin"], barrier),
                range(2),
            )
        )
    assert sum(isinstance(result, SectionRecount) for result in results) == 1
    conflicts = [result for result in results if isinstance(result, SectionRecountError)]
    assert len(conflicts) == 1
    assert "незавершённый" in str(conflicts[0])
    owner = SectionRecount.objects.get(status=SectionRecount.Status.COUNTING)
    assert (
        StockLocationLock.objects.filter(
            document_id=owner.pk, released_at__isnull=True
        ).count()
        == 10
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL row-lock integration test"
)
def test_postgresql_start_blocks_movement_deterministically(section_data):
    data = section_data
    target = StorageLocation.objects.get(code="S03-L03-D02-C02")
    entered = Event()
    release = Event()
    doc = create_section_recount(by=data["admin"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        start = pool.submit(_start_with_snapshot_gate, doc, entered, release)
        assert entered.wait(10)
        move = pool.submit(_parallel_call_captured, move_stock_lot, data["lot"], target)
        release.set()
        assert isinstance(start.result(), SectionRecount)
        move_result = move.result()
    assert isinstance(move_result, InventoryError)
    assert "заблокирован" in str(move_result)
    assert StockLot.objects.get(pk=data["lot"].pk).location_id == StorageLocation.objects.get(
        code="S03-L03-D02-C01"
    ).pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL row-lock integration test"
)
def test_postgresql_start_blocks_receiving_deterministically(section_data):
    data = section_data
    receiving_lot = create_stock_lot(
        data["batch_line"],
        StorageLocation.objects.get(code="S03-L03-D02-C03"),
        Decimal("1"),
    )
    entered = Event()
    release = Event()
    doc = create_section_recount(by=data["admin"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        start = pool.submit(_start_with_snapshot_gate, doc, entered, release)
        assert entered.wait(10)
        receiving = pool.submit(_parallel_call_captured, receive_stock_lot, receiving_lot)
        release.set()
        assert isinstance(start.result(), SectionRecount)
        receiving_result = receiving.result()
    assert isinstance(receiving_result, InventoryError)
    assert "заблокирован" in str(receiving_result)
    assert StockLot.objects.get(pk=receiving_lot.pk).status == StockLot.Status.RECEIVING


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL row-lock integration test"
)
def test_postgresql_two_applies_are_idempotent(section_data):
    doc = _ready(section_data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _parallel_call(apply_section_recount, doc), range(2)))
    assert all(isinstance(result, SectionRecount) for result in results)
    assert {result.status for result in results} == {SectionRecount.Status.COMPLETED}
    assert (
        StockMovement.objects.filter(
            document_type="section_recount", document_id=doc.pk
        ).count()
        == 2
    )


def test_cancel_releases_lock_and_completed_cannot_be_canceled(section_data):
    doc = _start(section_data)
    cancel_section_recount(doc)
    assert StockLocationLock.objects.filter(
        document_id=doc.pk, released_at__isnull=True
    ).count() == 0
    doc.refresh_from_db()
    assert doc.status == SectionRecount.Status.CANCELED

    completed = create_section_recount(by=section_data["admin"])
    completed.status = SectionRecount.Status.COMPLETED
    completed.save(update_fields=["status"])
    with pytest.raises(SectionRecountError):
        cancel_section_recount(completed)


def test_section_recount_requires_stocktaking_permission(client, django_user_model, admin):
    user = django_user_model.objects.create_user(username="seller", password="secret")
    client.force_login(user)
    assert client.get(reverse("section_recount_list")).status_code == 403

    client.force_login(admin)
    assert client.get(reverse("section_recount_list")).status_code == 200
