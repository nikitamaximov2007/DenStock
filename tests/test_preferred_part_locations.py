"""Regression coverage for durable preferred cells without fake stock."""

from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import (
    PartPreferredLocation,
    StockBalance,
    StockLot,
    StockMovement,
)
from apps.inventory.services import (
    create_stock_lot,
    perform_stock_transfer,
    receive_stock_lot,
    sell_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.fixture
def preferred_data(db, django_user_model):
    user = django_user_model.objects.create_superuser("preferred-admin", password="parol-12345")
    supplier = Supplier.objects.create(name="Поставщик закреплений")
    category = Category.objects.create(name="Закрепления")
    part = PartType.objects.create(
        name="Прокладка закрепления",
        category=category,
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=part, value="PREFERRED-CELL", is_primary=True)
    current = StorageLocation.objects.create(
        name="Текущая", code="S03-L03-D02-C09", storage_allowed=True, is_active=True
    )
    new = StorageLocation.objects.create(
        name="Новая", code="S03-L03-D02-C02", storage_allowed=True, is_active=True
    )
    other = StorageLocation.objects.create(
        name="Другая", code="S03-L03-D02-C03", storage_allowed=True, is_active=True
    )
    archived = StorageLocation.objects.create(
        name="Архив", code="S03-L03-D02-C04", storage_allowed=True, is_active=False
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal("5"),
        unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, user)
    line.refresh_from_db()
    return {
        "user": user,
        "part": part,
        "line": line,
        "current": current,
        "new": new,
        "other": other,
        "archived": archived,
    }


def _received_lot(data, *, location=None, quantity="2"):
    lot = create_stock_lot(data["line"], location or data["current"], Decimal(quantity))
    receive_stock_lot(lot, by=data["user"])
    return lot


def test_zero_sale_keeps_preferred_cell_without_balance_or_new_movement(preferred_data):
    lot = _received_lot(preferred_data)
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    before = StockMovement.objects.count()

    sell_stock_lot(lot, Decimal("2"), by=preferred_data["user"])

    lot.refresh_from_db()
    preference.refresh_from_db()
    assert lot.status == StockLot.Status.DEPLETED
    assert lot.quantity == 0
    assert preference.location == preferred_data["current"]
    assert not StockBalance.objects.filter(batch_line=preferred_data["line"]).exists()
    assert StockMovement.objects.count() == before + 1


def test_full_and_partial_transfers_update_preferred_cell_deterministically(preferred_data):
    lot = _received_lot(preferred_data, quantity="5")

    transfer, created = perform_stock_transfer(
        part=preferred_data["part"],
        from_location=preferred_data["current"],
        to_location=preferred_data["new"],
        quantity=Decimal("2"),
        stock_state=StockLot.Status.AVAILABLE,
        token="preferred-partial-transfer",
        by=preferred_data["user"],
    )

    assert created is True
    assert transfer.to_location == preferred_data["new"]
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    assert preference.location == preferred_data["new"]
    lot.refresh_from_db()
    assert lot.quantity == 3

    repeated, created = perform_stock_transfer(
        part=preferred_data["part"],
        from_location=preferred_data["current"],
        to_location=preferred_data["new"],
        quantity=Decimal("2"),
        stock_state=StockLot.Status.AVAILABLE,
        token="preferred-partial-transfer",
        by=preferred_data["user"],
    )
    assert created is False
    assert repeated.pk == transfer.pk
    assert PartPreferredLocation.objects.filter(part_type=preferred_data["part"]).count() == 1


def test_archived_preference_is_retained_as_history(preferred_data):
    _received_lot(preferred_data)
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    preferred_data["current"].is_active = False
    preferred_data["current"].save(update_fields=["is_active"])

    preference.refresh_from_db()
    assert preference.location == preferred_data["current"]
    assert preference.location.can_hold_stock() is False


def test_backfill_command_is_dry_run_then_creates_unambiguous_preferences(
    capsys, preferred_data
):
    _received_lot(preferred_data)
    PartPreferredLocation.objects.filter(part_type=preferred_data["part"]).delete()
    no_history = PartType.objects.create(
        name="Без истории",
        category=preferred_data["part"].category,
        unit=preferred_data["part"].unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )

    call_command("backfill_preferred_part_locations")
    output = capsys.readouterr().out
    assert "Dry-run only" in output
    assert "From one current cell: 1" in output
    assert not PartPreferredLocation.objects.filter(part_type=no_history).exists()

    call_command("backfill_preferred_part_locations", "--apply")

    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    assert preference.location == preferred_data["current"]
    assert not PartPreferredLocation.objects.filter(part_type=no_history).exists()


def test_backfill_leaves_multiple_current_cells_unassigned(preferred_data):
    _received_lot(preferred_data, quantity="2")
    second_batch = Batch.objects.create(supplier=Supplier.objects.create(name="Второй поставщик"))
    second_line = BatchLine.objects.create(
        batch=second_batch,
        part_type=preferred_data["part"],
        quantity=Decimal("1"),
        unit_cost_currency=Decimal("100"),
    )
    second_batch.status = Batch.Status.ACCEPTED
    second_batch.save(update_fields=["status"])
    finalize_cost(second_batch, preferred_data["user"])
    second_line.refresh_from_db()
    second_lot = create_stock_lot(second_line, preferred_data["other"], Decimal("1"))
    receive_stock_lot(second_lot, by=preferred_data["user"])
    PartPreferredLocation.objects.filter(part_type=preferred_data["part"]).delete()

    call_command("backfill_preferred_part_locations", "--apply")

    assert not PartPreferredLocation.objects.filter(part_type=preferred_data["part"]).exists()
