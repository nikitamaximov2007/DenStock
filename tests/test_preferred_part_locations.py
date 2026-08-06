"""Regression coverage for durable preferred cells without fake stock."""

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.urls import reverse

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
    write_off_stock_lot_quantity,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.receipts.services import ReceiptError, add_line, create_receipt, post_receipt, remove_line
from apps.stocktaking.services import (
    add_stock_lot_count_line,
    complete_inventory_count,
    create_inventory_count,
    update_counted_quantity,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from apps.warehouse.services import rename_storage_location


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
        "supplier": supplier,
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


def test_zero_write_off_keeps_preferred_cell_without_fake_stock(preferred_data):
    lot = _received_lot(preferred_data)
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    before = StockMovement.objects.count()

    write_off_stock_lot_quantity(lot, Decimal("2"), by=preferred_data["user"], comment="Тест")

    lot.refresh_from_db()
    preference.refresh_from_db()
    assert lot.status == StockLot.Status.WRITTEN_OFF
    assert lot.quantity == 0
    assert preference.location == preferred_data["current"]
    assert not StockBalance.objects.filter(batch_line=preferred_data["line"]).exists()
    assert StockMovement.objects.count() == before + 1


def test_zero_inventory_keeps_preferred_cell_without_fake_stock(preferred_data):
    lot = _received_lot(preferred_data)
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    doc = create_inventory_count(
        scope_location=preferred_data["current"], by=preferred_data["user"]
    )
    line = add_stock_lot_count_line(doc, lot, by=preferred_data["user"])
    update_counted_quantity(line, Decimal("0"), by=preferred_data["user"])

    complete_inventory_count(doc, by=preferred_data["user"])

    lot.refresh_from_db()
    preference.refresh_from_db()
    assert lot.status == StockLot.Status.DEPLETED
    assert lot.quantity == 0
    assert preference.location == preferred_data["current"]
    assert not StockBalance.objects.filter(batch_line=preferred_data["line"]).exists()


def test_full_transfer_relocates_preference_and_removes_only_empty_balance(preferred_data):
    lot = _received_lot(preferred_data)

    transfer, created = perform_stock_transfer(
        part=preferred_data["part"],
        from_location=preferred_data["current"],
        to_location=preferred_data["new"],
        quantity=Decimal("2"),
        stock_state=StockLot.Status.AVAILABLE,
        token="preferred-full-transfer",
        by=preferred_data["user"],
    )

    lot.refresh_from_db()
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    assert created is True
    assert transfer.to_location == preferred_data["new"]
    assert lot.status == StockLot.Status.DEPLETED
    assert lot.quantity == 0
    assert preference.location == preferred_data["new"]
    assert not StockBalance.objects.filter(
        batch_line=preferred_data["line"], location=preferred_data["current"]
    ).exists()
    assert StockBalance.objects.filter(
        part_type=preferred_data["part"], location=preferred_data["new"]
    ).exists()


def test_regular_receipt_guidance_offers_preferred_cell_after_zero_stock(client, preferred_data):
    lot = _received_lot(preferred_data)
    sell_stock_lot(lot, Decimal("2"), by=preferred_data["user"])
    before_movements = StockMovement.objects.count()

    client.force_login(preferred_data["user"])
    response = client.get(
        reverse("scanner_receiving_location_guidance"), {"part": preferred_data["part"].pk}
    )

    assert response.status_code == 200
    assert response.json() == {
        "mode": "preferred",
        "location": {
            "id": preferred_data["current"].pk,
            "code": preferred_data["current"].code,
            "name": preferred_data["current"].name,
        },
    }
    assert PartPreferredLocation.objects.get(part_type=preferred_data["part"]).location_id == (
        preferred_data["current"].pk
    )
    assert not StockBalance.objects.filter(batch_line=preferred_data["line"]).exists()
    assert StockMovement.objects.count() == before_movements


def test_regular_receipt_can_choose_another_cell_and_updates_only_after_post(preferred_data):
    lot = _received_lot(preferred_data)
    sell_stock_lot(lot, Decimal("2"), by=preferred_data["user"])
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    movements_before = StockMovement.objects.count()
    receipt = create_receipt(supplier=preferred_data["supplier"], by=preferred_data["user"])

    line = add_line(
        receipt,
        part_type=preferred_data["part"],
        quantity=Decimal("1"),
        unit_cost_rub=Decimal("10"),
        location=preferred_data["new"],
    )
    preference.refresh_from_db()
    assert line.location == preferred_data["new"]
    assert preference.location == preferred_data["current"]
    assert StockMovement.objects.count() == movements_before

    post_receipt(receipt, by=preferred_data["user"])

    preference.refresh_from_db()
    assert preference.location == preferred_data["new"]


def test_draft_receipt_removal_and_failed_post_leave_preference_unchanged(preferred_data):
    _received_lot(preferred_data)
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    receipt = create_receipt(supplier=preferred_data["supplier"], by=preferred_data["user"])
    line = add_line(
        receipt,
        part_type=preferred_data["part"],
        quantity=Decimal("1"),
        unit_cost_rub=Decimal("10"),
        location=preferred_data["new"],
    )

    remove_line(line)
    preference.refresh_from_db()
    assert preference.location == preferred_data["current"]

    add_line(
        receipt,
        part_type=preferred_data["part"],
        quantity=Decimal("1"),
        unit_cost_rub=Decimal("10"),
        location=preferred_data["new"],
    )
    preferred_data["new"].is_active = False
    preferred_data["new"].save(update_fields=["is_active"])

    with pytest.raises(ReceiptError):
        post_receipt(receipt, by=preferred_data["user"])

    preference.refresh_from_db()
    assert preference.location == preferred_data["current"]


def test_regular_receipt_guidance_requires_choice_for_multiple_current_cells(
    client, preferred_data
):
    lot = _received_lot(preferred_data)
    perform_stock_transfer(
        part=preferred_data["part"],
        from_location=preferred_data["current"],
        to_location=preferred_data["new"],
        quantity=Decimal("1"),
        stock_state=StockLot.Status.AVAILABLE,
        token="preferred-guidance-multiple",
        by=preferred_data["user"],
    )
    lot.refresh_from_db()
    assert lot.quantity == Decimal("1")

    client.force_login(preferred_data["user"])
    response = client.get(
        reverse("scanner_receiving_location_guidance"), {"part": preferred_data["part"].pk}
    )

    assert response.status_code == 200
    assert response.json() == {"mode": "multiple", "location": None}


def test_regular_receipt_guidance_skips_archived_preference_and_rename_keeps_link(
    client, preferred_data
):
    _received_lot(preferred_data)
    preference = PartPreferredLocation.objects.get(part_type=preferred_data["part"])
    old_code = preferred_data["current"].code
    renamed = rename_storage_location(
        preferred_data["current"],
        new_code="S03-L03-D02-C19",
        expected_code=old_code,
        by=preferred_data["user"],
    )
    preference.refresh_from_db()
    assert preference.location_id == renamed.pk
    assert preference.location.code == "S03-L03-D02-C19"

    renamed.is_active = False
    renamed.save(update_fields=["is_active"])
    client.force_login(preferred_data["user"])
    response = client.get(
        reverse("scanner_receiving_location_guidance"), {"part": preferred_data["part"].pk}
    )

    assert response.status_code == 200
    assert response.json() == {"mode": "preferred_unavailable", "location": None}


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
