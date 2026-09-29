"""A live PartItem's warehouse location changes only through the journal.

PartItemUpdateView used to be a plain Django UpdateView backed by
PartItemEditForm, saving `current_location` directly with no lock, no
movement, and no balance refresh - for ANY item, RECEIVING or long since
sold. Direct location editing now exists only for a fresh RECEIVING item with
no movements; a live item's location must go through move_part_item. Serial
number and note stay editable at any time - they are not warehouse physics.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import PartItem, StockBalance, StockMovement
from apps.inventory.services import (
    InventoryError,
    change_part_item_status,
    check_stock_balance,
    create_part_items,
    item_is_directly_editable,
    move_part_item,
    receive_part_item,
    update_part_item,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
MOVE = StockMovement.MovementType.MOVE_ITEM


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="edit-admin", password=PASSWORD)


@pytest.fixture
def storekeeper(db, django_user_model):
    user = django_user_model.objects.create_user(username="edit-keeper", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    return user


@pytest.fixture
def refs(db):
    part = PartType.objects.create(
        name="Насос", category=Category.objects.create(name="Двигатель"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.SERIAL,
    )
    cell = StorageLocation.objects.create(
        name="Ячейка A", code="S02-D01-C01", storage_allowed=True, is_active=True
    )
    other = StorageLocation.objects.create(
        name="Ячейка B", code="S02-D01-C02", storage_allowed=True, is_active=True
    )
    return {"sup": Supplier.objects.create(name="Поставщик"), "part": part,
            "cell": cell, "other": other}


def _line(refs, admin, quantity="2"):
    batch = Batch.objects.create(supplier=refs["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=refs["part"], quantity=Decimal(quantity),
        unit_cost_currency=Decimal("50"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


def _receiving_item(refs, admin, *, located=True):
    line = _line(refs, admin)
    location = refs["cell"] if located else None
    return create_part_items(line, 1, current_location=location)[0]


def _live_item(refs, admin):
    return receive_part_item(_receiving_item(refs, admin), by=admin)


# --- A. RECEIVING item with no movements: location edit still succeeds -------


def test_fresh_receiving_item_without_movements_can_still_be_edited(refs, admin):
    item = _receiving_item(refs, admin)
    assert item_is_directly_editable(item)

    update_part_item(
        item, serial_number="SN-1", current_location=refs["other"], note="пересчёт"
    )

    item.refresh_from_db()
    assert (item.serial_number, item.current_location, item.note) == (
        "SN-1", refs["other"], "пересчёт"
    )
    assert not StockMovement.objects.filter(part_item=item).exists()


# --- B. AVAILABLE item: location edit refused, nothing changes ---------------


def test_available_item_location_edit_is_refused(refs, admin):
    item = _live_item(refs, admin)

    with pytest.raises(InventoryError, match="Переместить"):
        update_part_item(item, current_location=refs["other"])

    item.refresh_from_db()
    assert item.current_location == refs["cell"]
    assert not StockMovement.objects.filter(part_item=item, movement_type=MOVE).exists()
    balance = StockBalance.objects.get(batch_line=item.batch_line, location=refs["cell"])
    assert balance.quantity_available == Decimal("1")
    assert check_stock_balance() == []


def test_note_and_serial_of_a_live_item_can_change_without_touching_location(refs, admin):
    item = _live_item(refs, admin)

    update_part_item(
        item, serial_number="SN-9", current_location=item.current_location, note="верхняя полка"
    )

    item.refresh_from_db()
    assert (item.serial_number, item.note) == ("SN-9", "верхняя полка")
    assert item.current_location == refs["cell"]
    assert check_stock_balance() == []


# --- C. Any movement history closes direct editing, even without leaving RECEIVING --


def test_receiving_item_with_a_movement_is_no_longer_directly_editable(refs, admin):
    item = _receiving_item(refs, admin)
    received = receive_part_item(item, by=admin)
    moved = move_part_item(received, refs["other"], by=admin)

    assert not item_is_directly_editable(moved)
    with pytest.raises(InventoryError):
        update_part_item(moved, current_location=refs["cell"])
    moved.refresh_from_db()
    assert moved.current_location == refs["other"]


def test_quarantined_item_location_edit_is_refused(refs, admin):
    item = _live_item(refs, admin)
    change_part_item_status(item, PartItem.Status.QUARANTINE, by=admin)
    item.refresh_from_db()

    assert not item_is_directly_editable(item)
    with pytest.raises(InventoryError):
        update_part_item(item, current_location=refs["other"])
    item.refresh_from_db()
    assert item.current_location == refs["cell"]


# --- D. Crafted POST: server-side guard, not just hiding the field -----------


def test_crafted_edit_post_cannot_teleport_a_live_item(client, refs, admin, storekeeper):
    item = _live_item(refs, admin)
    client.force_login(storekeeper)

    response = client.post(
        reverse("item_edit", args=[item.pk]),
        {"serial_number": "", "current_location": refs["other"].pk, "note": "взлом"},
        follow=True,
    )

    assert "Переместить" in response.content.decode()
    item.refresh_from_db()
    assert item.current_location == refs["cell"]
    assert item.note == ""
    balance = StockBalance.objects.get(batch_line=item.batch_line, location=refs["cell"])
    assert balance.quantity_available == Decimal("1")
    assert check_stock_balance() == []


def test_edit_page_still_lets_a_live_item_fix_its_note(client, refs, admin):
    item = _live_item(refs, admin)
    client.force_login(admin)

    response = client.post(
        reverse("item_edit", args=[item.pk]),
        {"serial_number": "", "current_location": item.current_location_id, "note": "проверено"},
        follow=True,
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.note == "проверено"
    assert item.current_location == refs["cell"]


def test_canonical_move_still_works_and_leaves_a_movement(client, refs, admin):
    item = _live_item(refs, admin)
    client.force_login(admin)

    client.post(reverse("item_move", args=[item.pk]), {"to_location": refs["other"].pk})

    item.refresh_from_db()
    assert item.current_location == refs["other"]
    assert StockMovement.objects.filter(part_item=item, movement_type=MOVE).exists()
    assert check_stock_balance() == []


def test_edit_form_reachable_and_usable_for_a_fresh_receiving_item(client, refs, admin):
    item = _receiving_item(refs, admin)
    client.force_login(admin)

    assert client.get(reverse("item_edit", args=[item.pk])).status_code == 200
    response = client.post(
        reverse("item_edit", args=[item.pk]),
        {"serial_number": "SN-2", "current_location": refs["other"].pk, "note": ""},
        follow=True,
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.current_location == refs["other"]
    assert item.serial_number == "SN-2"
