"""Безопасный workflow полного пересчёта фиксированного участка."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockBalance, StockLocationLock, StockLot, StockMovement
from apps.inventory.services import InventoryError, create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.stocktaking.models import SectionRecount
from apps.stocktaking.section_recount import (
    SectionRecountError,
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
    part = PartType.objects.create(
        name="Деталь пересчёта",
        category=category,
        unit=Unit.objects.get(name="Штука"),
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


def test_snapshot_change_fails_without_partial_apply(section_data):
    data = section_data
    doc = _start(data)
    record_section_scan(doc, cell_number=2, raw_value="RC-0001", by=data["admin"])
    _complete_all_cells(doc)
    doc = mark_section_ready(doc)
    StockLot.objects.filter(pk=data["lot"].pk).update(quantity=Decimal("4"))

    with pytest.raises(SectionRecountError, match="snapshot"):
        apply_section_recount(doc)
    doc.refresh_from_db()
    assert doc.status == SectionRecount.Status.FAILED
    assert StockMovement.objects.filter(
        document_type="section_recount", document_id=doc.pk
    ).count() == 0
    assert StockLot.objects.get(pk=data["lot"].pk).quantity == Decimal("4")


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
