"""Повседневный пересчёт одной произвольной ячейки и безопасное архивирование."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from importlib import import_module
from threading import Barrier
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection
from django.db.models import Q
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import (
    PartPreferredLocation,
    StockLocationLock,
    StockLot,
    StockMovement,
)
from apps.inventory.services import (
    InventoryError,
    adjust_stock_lot_quantity,
    create_stock_lot,
    move_stock_lot,
    receive_stock_lot,
    set_preferred_part_location,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Reservation, ReservationLine
from apps.sales.services import (
    ReservationError,
    SaleError,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    complete_sale,
    create_reservation,
    create_sale,
)
from apps.stocktaking.models import SectionRecount
from apps.stocktaking.section_recount import (
    SectionRecountError,
    allocate_section_line,
    apply_section_recount,
    complete_section_cell,
    create_cell_recount,
    mark_section_ready,
    record_section_part,
    record_section_scan,
    remove_section_line,
    set_section_line_quantity,
)
from apps.stocktaking.services import (
    StocktakingError,
    add_stock_lot_count_line,
    complete_inventory_count,
    create_inventory_count,
    update_counted_quantity,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from apps.warehouse.services import (
    StorageLocationRemovalError,
    remove_or_archive_storage_location,
    storage_location_removal_preview,
)
from apps.writeoffs.models import WriteOffDocument
from apps.writeoffs.services import (
    WriteOffError,
    add_stock_lot_to_write_off,
    complete_write_off,
    create_write_off,
)


@pytest.fixture
def cell_admin(db, django_user_model):
    return django_user_model.objects.create_superuser(
        username="cell-recount-admin", password="secret"
    )


def _part_with_batch(*, supplier, category, unit, admin, number, quantity="10", cost="100"):
    part = PartType.objects.create(
        name=f"Деталь {number}",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=part, value=number, kind=PartNumber.Kind.OEM, is_primary=True
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal(cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return part, line


@pytest.fixture
def cell_data(db, cell_admin):
    supplier = Supplier.objects.create(name="Поставщик ячейки")
    category = Category.objects.create(name="Категория ячейки")
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    location = StorageLocation.objects.create(
        code="S09-L02-D04-C07", name="Произвольная ячейка"
    )
    other = StorageLocation.objects.create(code="S09-L02-D04-C08", name="Соседняя")
    part, batch_line = _part_with_batch(
        supplier=supplier,
        category=category,
        unit=unit,
        admin=cell_admin,
        number="CELL-0001",
    )
    lot = create_stock_lot(batch_line, location, Decimal("5"))
    receive_stock_lot(lot, by=cell_admin)
    return {
        "admin": cell_admin,
        "supplier": supplier,
        "category": category,
        "unit": unit,
        "location": location,
        "other": other,
        "part": part,
        "batch_line": batch_line,
        "lot": lot,
    }


def _ready(data, quantity):
    doc = create_cell_recount(location=data["location"], by=data["admin"])
    line = None
    if Decimal(quantity) > 0:
        line = record_section_scan(
            doc, cell_number=1, raw_value="CELL-0001", by=data["admin"]
        )
        set_section_line_quantity(line, quantity, by=data["admin"])
    complete_section_cell(doc, cell_number=1, by=data["admin"])
    return mark_section_ready(doc), line


def _document_movements(doc):
    return StockMovement.objects.filter(
        document_type="section_recount", document_id=doc.pk
    )


def _create_cell_concurrently(location_id, user_id, barrier):
    from django.contrib.auth import get_user_model

    close_old_connections()
    try:
        barrier.wait(10)
        return create_cell_recount(
            location=StorageLocation.objects.get(pk=location_id),
            by=get_user_model().objects.get(pk=user_id),
        )
    except Exception as exc:  # noqa: BLE001 - тест проверяет точный исход гонки.
        return exc
    finally:
        close_old_connections()


def test_cell_recount_no_difference_creates_no_adjustments(cell_data):
    doc, _line = _ready(cell_data, "5")
    assert doc.result["matched_positions"] == 1
    assert doc.result["adjust_out"] == "0"
    assert doc.result["adjust_in"] == "0"

    completed = apply_section_recount(doc)

    assert completed.result["created_movements"] == 0
    assert _document_movements(doc).count() == 0
    assert StockLot.objects.get(pk=cell_data["lot"].pk).quantity == Decimal("5")


@pytest.mark.parametrize(
    ("counted", "movement_type", "delta"),
    [
        ("4", StockMovement.MovementType.ADJUST_OUT, Decimal("1")),
        ("6", StockMovement.MovementType.ADJUST_IN, Decimal("1")),
    ],
)
def test_cell_recount_applies_only_shortage_or_excess_delta(
    cell_data, counted, movement_type, delta
):
    doc, _line = _ready(cell_data, counted)
    apply_section_recount(doc)

    movement = _document_movements(doc).get()
    assert movement.movement_type == movement_type
    assert movement.quantity == delta
    assert StockLot.objects.get(pk=cell_data["lot"].pk).quantity == Decimal(counted)


def test_cell_recount_records_actual_apply_actor(cell_data, django_user_model):
    approver = django_user_model.objects.create_superuser(
        username="cell-recount-approver", password="secret"
    )
    doc, _line = _ready(cell_data, "4")

    completed = apply_section_recount(doc, by=approver)

    assert _document_movements(doc).get().created_by == approver
    assert completed.result["applied_by"] == {
        "id": approver.pk,
        "username": approver.get_username(),
    }


def test_cell_recount_physical_empty_adjusts_everything_out(cell_data):
    doc, _line = _ready(cell_data, "0")
    assert doc.result["comparison_rows"][0]["status"] == "system_only"

    apply_section_recount(doc)

    assert StockLot.objects.get(pk=cell_data["lot"].pk).quantity == 0
    assert _document_movements(doc).get().movement_type == StockMovement.MovementType.ADJUST_OUT


def test_empty_system_cell_found_part_requires_valid_explicit_batch(cell_data):
    doc = create_cell_recount(location=cell_data["other"], by=cell_data["admin"])
    line = record_section_scan(doc, cell_number=1, raw_value="CELL-0001")
    set_section_line_quantity(line, "2")
    complete_section_cell(doc, cell_number=1)
    with pytest.raises(SectionRecountError, match="распределить партии"):
        mark_section_ready(doc)

    allocate_section_line(
        line,
        batch_line_id=cell_data["batch_line"].pk,
        quantity="2",
        lot_status=StockLot.Status.AVAILABLE,
    )
    ready = mark_section_ready(doc)
    assert ready.result["comparison_rows"][0]["status"] == "actual_only"
    apply_section_recount(ready)
    found = StockLot.objects.get(
        batch_line=cell_data["batch_line"], location=cell_data["other"]
    )
    assert found.quantity == Decimal("2")
    assert found.landed_unit_cost_rub == cell_data["batch_line"].landed_unit_cost_rub


def test_found_excess_cannot_exceed_global_batch_capacity(cell_data):
    second = create_stock_lot(cell_data["batch_line"], cell_data["other"], Decimal("5"))
    receive_stock_lot(second, by=cell_data["admin"])
    empty = StorageLocation.objects.create(code="S09-L02-D04-C09", name="Пустая")
    doc = create_cell_recount(location=empty, by=cell_data["admin"])
    line = record_section_scan(doc, cell_number=1, raw_value="CELL-0001")
    with pytest.raises(SectionRecountError, match="лимита"):
        allocate_section_line(
            line,
            batch_line_id=cell_data["batch_line"].pk,
            quantity="1",
            lot_status=StockLot.Status.AVAILABLE,
        )


def test_multiple_parts_are_compared_independently(cell_data):
    second_part, second_line = _part_with_batch(
        supplier=cell_data["supplier"],
        category=cell_data["category"],
        unit=cell_data["unit"],
        admin=cell_data["admin"],
        number="CELL-0002",
    )
    second_lot = create_stock_lot(second_line, cell_data["location"], Decimal("2"))
    receive_stock_lot(second_lot, by=cell_data["admin"])
    doc = create_cell_recount(location=cell_data["location"], by=cell_data["admin"])
    first = record_section_scan(doc, cell_number=1, raw_value="CELL-0001")
    set_section_line_quantity(first, "5")
    second = record_section_scan(doc, cell_number=1, raw_value="CELL-0002")
    set_section_line_quantity(second, "1")
    complete_section_cell(doc, cell_number=1)
    ready = mark_section_ready(doc)

    assert {row["part_type_id"] for row in ready.result["comparison_rows"]} == {
        cell_data["part"].pk,
        second_part.pk,
    }
    assert ready.result["matched_positions"] == 1
    assert ready.result["shortage_positions"] == 1


def test_same_part_multiple_batches_and_costs_require_explicit_distribution(cell_data):
    second_batch = Batch.objects.create(supplier=cell_data["supplier"])
    second_line = BatchLine.objects.create(
        batch=second_batch,
        part_type=cell_data["part"],
        quantity=Decimal("4"),
        unit_cost_currency=Decimal("170"),
    )
    second_batch.status = Batch.Status.ACCEPTED
    second_batch.save(update_fields=["status"])
    finalize_cost(second_batch, cell_data["admin"])
    second_line.refresh_from_db()
    second_lot = create_stock_lot(second_line, cell_data["location"], Decimal("2"))
    receive_stock_lot(second_lot, by=cell_data["admin"])
    doc = create_cell_recount(location=cell_data["location"], by=cell_data["admin"])
    line = record_section_scan(doc, cell_number=1, raw_value="CELL-0001")
    set_section_line_quantity(line, "7")
    complete_section_cell(doc, cell_number=1)
    with pytest.raises(SectionRecountError, match="партии"):
        mark_section_ready(doc)

    first_allocation = allocate_section_line(
        line,
        batch_line_id=cell_data["batch_line"].pk,
        quantity="5",
        lot_status=StockLot.Status.AVAILABLE,
    )
    second_allocation = allocate_section_line(
        line,
        batch_line_id=second_line.pk,
        quantity="2",
        lot_status=StockLot.Status.AVAILABLE,
    )
    ready = mark_section_ready(doc)
    assert first_allocation.unit_cost_rub != second_allocation.unit_cost_rub
    assert ready.result["adjust_out"] == "0"
    assert ready.result["adjust_in"] == "0"


def test_quarantine_status_is_preserved(cell_data):
    StockLot.objects.filter(pk=cell_data["lot"].pk).update(status=StockLot.Status.QUARANTINE)
    doc, _line = _ready(cell_data, "4")
    apply_section_recount(doc)
    cell_data["lot"].refresh_from_db()
    assert cell_data["lot"].status == StockLot.Status.QUARANTINE
    assert cell_data["lot"].quantity == Decimal("4")


def test_scan_plus_one_manual_quantity_and_remove_line(cell_data):
    doc = create_cell_recount(location=cell_data["location"], by=cell_data["admin"])
    line = record_section_scan(doc, cell_number=1, raw_value="CELL-0001")
    line = record_section_scan(doc, cell_number=1, raw_value="CELL-0001")
    assert line.quantity == 2
    set_section_line_quantity(line, "3")
    line.refresh_from_db()
    assert line.quantity == 3
    remove_section_line(line)
    assert doc.lines.count() == 0

    unnamed = PartType.objects.create(
        name="Деталь без номера",
        category=cell_data["category"],
        unit=cell_data["unit"],
        tracking_mode=PartType.TrackingMode.BULK,
    )
    selected = record_section_part(doc, cell_number=1, part_id=unnamed.pk)
    assert selected.part_type == unnamed
    assert selected.part_number == str(unnamed.pk)


def test_repeated_finish_and_apply_are_idempotent(cell_data):
    doc = create_cell_recount(location=cell_data["location"], by=cell_data["admin"])
    line = record_section_scan(doc, cell_number=1, raw_value="CELL-0001")
    set_section_line_quantity(line, "4")
    complete_section_cell(doc, cell_number=1)
    complete_section_cell(doc, cell_number=1)
    ready = mark_section_ready(doc)
    apply_section_recount(ready)
    apply_section_recount(ready)
    assert _document_movements(doc).count() == 1


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL row-lock integration test"
)
def test_postgresql_cell_recount_concurrent_start_has_single_owner(cell_data):
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _create_cell_concurrently,
                cell_data["location"].pk,
                cell_data["admin"].pk,
                barrier,
            )
            for _ in range(2)
        ]
        results = [future.result() for future in futures]
    assert sum(isinstance(result, SectionRecount) for result in results) == 1
    conflicts = [result for result in results if isinstance(result, SectionRecountError)]
    assert len(conflicts) == 1
    owner = SectionRecount.objects.get(status=SectionRecount.Status.COUNTING)
    assert owner.scope == SectionRecount.Scope.CELL
    assert StockLocationLock.objects.filter(
        document_id=owner.pk, released_at__isnull=True
    ).count() == 1


def test_snapshot_mismatch_leaves_stock_unchanged(cell_data):
    doc, _line = _ready(cell_data, "4")
    StorageLocation.objects.filter(pk=cell_data["location"].pk).update(name="Изменено")
    before = StockMovement.objects.count()
    with pytest.raises(SectionRecountError, match="Snapshot"):
        apply_section_recount(doc)
    assert StockLot.objects.get(pk=cell_data["lot"].pk).quantity == 5
    assert StockMovement.objects.count() == before


def test_batch_metadata_change_after_snapshot_blocks_apply(cell_data):
    doc, _line = _ready(cell_data, "4")
    BatchLine.objects.filter(pk=cell_data["batch_line"].pk).update(quantity=Decimal("9"))
    before = StockMovement.objects.count()
    with pytest.raises(SectionRecountError, match="Snapshot"):
        apply_section_recount(doc)
    assert StockLot.objects.get(pk=cell_data["lot"].pk).quantity == Decimal("5")
    assert StockMovement.objects.count() == before


def test_migration_backfills_existing_recount_snapshot_fields(cell_data):
    set_preferred_part_location(
        cell_data["part"], cell_data["location"], by=cell_data["admin"]
    )
    doc, line = _ready(cell_data, "5")
    allocation = line.allocations.get()
    type(allocation).objects.filter(pk=allocation.pk).update(
        batch_quantity_snapshot=Decimal("0"),
        batch_updated_at_snapshot=doc.created_at,
    )
    type(line).objects.filter(pk=line.pk).update(preferred_snapshot={})

    migration = import_module(
        "apps.stocktaking.migrations.0006_sectionrecount_scope_and_more"
    )
    migration.backfill_recount_snapshots(django_apps, None)

    allocation.refresh_from_db()
    line.refresh_from_db()
    assert allocation.batch_quantity_snapshot == cell_data["batch_line"].quantity
    assert allocation.batch_updated_at_snapshot == cell_data["batch_line"].updated_at
    assert line.preferred_snapshot["location_id"] == cell_data["location"].pk


def test_mid_apply_fault_rolls_back_stock_and_movements(cell_data):
    doc, _line = _ready(cell_data, "4")
    before = StockMovement.objects.count()
    with patch(
        "apps.stocktaking.section_recount.adjust_stock_lot_quantity",
        side_effect=RuntimeError("fault"),
    ):
        with pytest.raises(SectionRecountError, match="откатилось"):
            apply_section_recount(doc)
    assert StockLot.objects.get(pk=cell_data["lot"].pk).quantity == 5
    assert StockMovement.objects.count() == before


def test_reservation_blocks_ready_and_location_lock_blocks_movement_receiving(cell_data):
    reservation = Reservation.objects.create(
        customer_name="Клиент", status=Reservation.Status.ACTIVE
    )
    ReservationLine.objects.create(
        reservation=reservation,
        part_type=cell_data["part"],
        stock_lot=cell_data["lot"],
        quantity=Decimal("1"),
    )
    doc = create_cell_recount(location=cell_data["location"], by=cell_data["admin"])
    complete_section_cell(doc, cell_number=1)
    with pytest.raises(SectionRecountError, match="резервы"):
        mark_section_ready(doc)
    reservation.lines.all().delete()
    with pytest.raises(InventoryError, match="заблокирована"):
        move_stock_lot(cell_data["lot"], cell_data["other"])

    extra_part, extra_line = _part_with_batch(
        supplier=cell_data["supplier"],
        category=cell_data["category"],
        unit=cell_data["unit"],
        admin=cell_data["admin"],
        number="CELL-RECEIVE",
    )
    assert extra_part.pk
    with pytest.raises(InventoryError, match="заблокирована"):
        create_stock_lot(extra_line, cell_data["location"], Decimal("1"))


def test_cell_lock_blocks_reservation_sale_writeoff_and_inventory(cell_data):
    doc = create_cell_recount(location=cell_data["location"], by=cell_data["admin"])
    reservation = create_reservation(customer_name="Новый клиент", by=cell_data["admin"])
    with pytest.raises((ReservationError, InventoryError), match="заблокирована"):
        add_stock_lot_to_reservation(reservation, cell_data["lot"], Decimal("1"))

    sale = create_sale(customer_name="Покупатель", by=cell_data["admin"])
    add_stock_lot_to_sale(sale, cell_data["lot"], Decimal("1"), unit_price=Decimal("200"))
    with pytest.raises((SaleError, InventoryError), match="заблокирована"):
        complete_sale(sale, by=cell_data["admin"])

    write_off = create_write_off(
        reason=WriteOffDocument.Reason.OTHER, by=cell_data["admin"]
    )
    add_stock_lot_to_write_off(write_off, cell_data["lot"], Decimal("1"))
    with pytest.raises((WriteOffError, InventoryError), match="заблокирована"):
        complete_write_off(write_off, by=cell_data["admin"])

    inventory = create_inventory_count(
        scope_location=cell_data["location"], by=cell_data["admin"]
    )
    inventory_line = add_stock_lot_count_line(inventory, cell_data["lot"])
    update_counted_quantity(inventory_line, Decimal("4"))
    with pytest.raises(StocktakingError, match="заблокирована"):
        complete_inventory_count(inventory, by=cell_data["admin"])
    assert StockLot.objects.get(pk=cell_data["lot"].pk).quantity == Decimal("5")
    assert _document_movements(doc).count() == 0


def test_preferred_location_updates_only_when_unambiguous_and_survives_zero(cell_data):
    set_preferred_part_location(cell_data["part"], cell_data["other"], by=cell_data["admin"])
    doc, _line = _ready(cell_data, "5")
    apply_section_recount(doc)
    assert PartPreferredLocation.objects.get(part_type=cell_data["part"]).location == cell_data[
        "location"
    ]

    zero_doc, _line = _ready(cell_data, "0")
    apply_section_recount(zero_doc)
    assert PartPreferredLocation.objects.get(part_type=cell_data["part"]).location == cell_data[
        "location"
    ]


def test_arbitrary_cell_ui_scanner_search_and_permissions(cell_data, client, django_user_model):
    client.force_login(cell_data["admin"])
    start_url = reverse("cell_recount_new", args=[cell_data["location"].pk])
    page = client.get(start_url)
    assert page.status_code == 200
    assert "Пересчитать ячейку" in page.content.decode()
    response = client.post(start_url)
    doc = SectionRecount.objects.get(scope=SectionRecount.Scope.CELL)
    assert response.status_code == 302
    detail = client.get(reverse("section_recount_detail", args=[doc.pk]), {"q": "Деталь"})
    html = detail.content.decode()
    assert 'name="raw_value"' in html
    assert 'name="q"' in html
    assert 'name="viewport"' in html
    assert doc.section_code == "S09-L02-D04-C07"

    viewer = django_user_model.objects.create_user(username="cell-viewer", password="secret")
    viewer.groups.add(Group.objects.get(name=roles.VIEWER))
    client.force_login(viewer)
    assert client.get(start_url).status_code == 403


def test_cell_recount_rejects_non_cell_location(cell_data, client):
    drawer = StorageLocation.objects.create(
        code="S10-L01-D01",
        name="Не ячейка",
        level=StorageLocation.Level.DRAWER,
        storage_allowed=True,
    )
    client.force_login(cell_data["admin"])

    page = client.get(reverse("location_detail", args=[drawer.pk]))
    assert "Пересчитать ячейку" not in page.content.decode()
    assert client.get(reverse("cell_recount_new", args=[drawer.pk])).status_code == 404
    with pytest.raises(SectionRecountError, match="только активную складскую ячейку"):
        create_cell_recount(location=drawer, by=cell_data["admin"])


def test_hard_delete_only_new_unused_empty_location(cell_data):
    new_location = StorageLocation.objects.create(code="S10-L01-D01-C01", name="Новая")
    preview = storage_location_removal_preview(new_location)
    assert preview["can_hard_delete"] is True
    result, code = remove_or_archive_storage_location(
        new_location, action="delete", expected_code=new_location.code
    )
    assert (result, code) == ("deleted", "S10-L01-D01-C01")
    assert not StorageLocation.objects.filter(pk=new_location.pk).exists()


def test_delete_with_stock_is_blocked(cell_data):
    preview = storage_location_removal_preview(cell_data["location"])
    assert preview["has_stock"] is True
    with pytest.raises(StorageLocationRemovalError, match="остаток"):
        remove_or_archive_storage_location(
            cell_data["location"],
            action="archive",
            expected_code=cell_data["location"].code,
        )


def test_historical_empty_location_is_archived_and_movement_history_survives(cell_data):
    adjust_stock_lot_quantity(
        cell_data["lot"],
        Decimal("-5"),
        by=cell_data["admin"],
        comment="Подготовка пустой исторической ячейки",
    )
    movement_ids = list(
        StockMovement.objects.filter(
            Q(from_location=cell_data["location"]) | Q(to_location=cell_data["location"])
        ).values_list("pk", flat=True)
    )
    preview = storage_location_removal_preview(cell_data["location"])
    assert preview["can_hard_delete"] is False
    assert preview["can_archive"] is True
    with pytest.raises(StorageLocationRemovalError, match="Hard delete"):
        remove_or_archive_storage_location(
            cell_data["location"],
            action="delete",
            expected_code=cell_data["location"].code,
        )

    result, _code = remove_or_archive_storage_location(
        cell_data["location"],
        action="archive",
        expected_code=cell_data["location"].code,
    )
    cell_data["location"].refresh_from_db()
    assert result == "archived"
    assert cell_data["location"].is_active is False
    assert cell_data["location"].storage_allowed is False
    remaining_movements = list(
        StockMovement.objects.filter(pk__in=movement_ids).values_list("pk", flat=True)
    )
    assert remaining_movements == movement_ids


def test_remove_requires_exact_code_and_active_recount_blocks_archive(cell_data):
    empty = StorageLocation.objects.create(code="S10-L01-D01-C02", name="Новая")
    with pytest.raises(StorageLocationRemovalError, match="точный код"):
        remove_or_archive_storage_location(empty, action="delete", expected_code="wrong")
    doc = create_cell_recount(location=empty, by=cell_data["admin"])
    assert StockLocationLock.objects.filter(document_id=doc.pk, released_at__isnull=True).exists()
    with pytest.raises(StorageLocationRemovalError, match="Hard delete"):
        remove_or_archive_storage_location(
            empty, action="delete", expected_code=empty.code
        )


def test_location_remove_page_uses_confirmation_and_permission(cell_data, client):
    client.force_login(cell_data["admin"])
    assert client.get(reverse("warehouse_index")).status_code == 200
    url = reverse("location_remove", args=[cell_data["location"].pk])
    html = client.get(url).content.decode()
    assert "Вы уверены, что хотите удалить ячейку" in html
    assert "Действие запрещено" in html
    empty = StorageLocation.objects.create(code="S10-L01-D01-C03", name="Новая")
    empty_html = client.get(reverse("location_remove", args=[empty.pk])).content.decode()
    assert 'name="expected_code"' in empty_html

    shelving = StorageLocation.objects.create(
        code="S10", name="Стеллаж", level=StorageLocation.Level.RACK
    )
    assert client.get(reverse("location_remove", args=[shelving.pk])).status_code == 404
