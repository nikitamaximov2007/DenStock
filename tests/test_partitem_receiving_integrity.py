"""A serialized PartItem leaves RECEIVING only through the movement journal.

The generic status buttons let a RECEIVING item become «Доступен» by directly
assigning `item.status`, with no receipt movement, no balance-cache refresh,
and no preferred-location update - the exact serialized-item equivalent of the
already-fixed StockLot bug (`inventory: lot quantity and cell change only
through the journal`). Leaving receiving now always goes through
`receive_part_item`, and every status change refreshes the cache.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import PartItem, PartPreferredLocation, StockBalance, StockMovement
from apps.inventory.services import (
    InventoryError,
    change_part_item_status,
    check_stock_balance,
    create_part_items,
    receive_part_item,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
RECEIPT = StockMovement.MovementType.RECEIVE_ITEM


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="item-admin", password=PASSWORD)


@pytest.fixture
def storekeeper(db, django_user_model):
    user = django_user_model.objects.create_user(username="item-keeper", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    return user


@pytest.fixture
def refs(db):
    part = PartType.objects.create(
        name="Насос", category=Category.objects.create(name="Двигатель"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.SERIAL,
    )
    cell = StorageLocation.objects.create(
        name="Ячейка A", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    other = StorageLocation.objects.create(
        name="Ячейка B", code="S01-D01-C02", storage_allowed=True, is_active=True
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


def _split(balance):
    return balance.quantity_available, balance.quantity_quarantine


def _assert_ledger_matches(item):
    """Item state = movement journal = balance cache."""
    item.refresh_from_db()
    if item.status == PartItem.Status.RECEIVING:
        assert not StockMovement.objects.filter(part_item=item, movement_type=RECEIPT).exists()
    else:
        receipts = StockMovement.objects.filter(part_item=item, movement_type=RECEIPT)
        assert receipts.count() == 1
    assert check_stock_balance() == []


# --- Leaving RECEIVING always goes through receive_part_item -----------------


def test_receiving_to_available_writes_one_receipt_and_the_cache(client, refs, admin, storekeeper):
    item = _receiving_item(refs, admin)
    client.force_login(storekeeper)

    client.post(reverse("item_status_change", args=[item.pk]), {"status": "available"})
    client.post(reverse("item_status_change", args=[item.pk]), {"status": "available"})

    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert item.current_location == refs["cell"]
    assert StockMovement.objects.filter(part_item=item, movement_type=RECEIPT).count() == 1
    balance = StockBalance.objects.get(batch_line=item.batch_line, location=refs["cell"])
    assert balance.quantity_available == Decimal("1")
    assert PartPreferredLocation.objects.get(part_type=refs["part"]).location == refs["cell"]
    _assert_ledger_matches(item)


def test_retry_does_not_create_a_second_receipt_or_double_stock(refs, admin):
    item = _receiving_item(refs, admin)

    change_part_item_status(item, PartItem.Status.AVAILABLE, by=admin)
    change_part_item_status(item, PartItem.Status.AVAILABLE, by=admin)

    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert StockMovement.objects.filter(part_item=item, movement_type=RECEIPT).count() == 1
    balance = StockBalance.objects.get(batch_line=item.batch_line, location=refs["cell"])
    assert balance.quantity_available == Decimal("1")
    _assert_ledger_matches(item)


def test_receiving_item_without_a_location_is_refused_not_silently_received(refs, admin):
    item = _receiving_item(refs, admin, located=False)

    with pytest.raises(InventoryError, match="ячейка приёмки"):
        change_part_item_status(item, PartItem.Status.AVAILABLE, by=admin)

    item.refresh_from_db()
    assert item.status == PartItem.Status.RECEIVING
    assert not StockMovement.objects.filter(part_item=item, movement_type=RECEIPT).exists()


def test_receiving_item_offers_accept_not_a_second_available_button(client, refs, admin):
    item = _receiving_item(refs, admin)
    client.force_login(admin)
    detail = client.get(reverse("item_detail", args=[item.pk])).content.decode()
    assert reverse("item_receive", args=[item.pk]) in detail
    assert 'name="status" value="available"' not in detail
    assert 'name="status" value="quarantine"' in detail


def test_receiving_to_quarantine_is_received_first(refs, admin):
    item = _receiving_item(refs, admin)

    change_part_item_status(item, PartItem.Status.QUARANTINE, by=admin)

    item.refresh_from_db()
    assert item.status == PartItem.Status.QUARANTINE
    assert item.current_location == refs["cell"]
    assert StockMovement.objects.filter(part_item=item, movement_type=RECEIPT).count() == 1
    balance = StockBalance.objects.get(batch_line=item.batch_line, location=refs["cell"])
    assert _split(balance) == (Decimal("0"), Decimal("1"))
    _assert_ledger_matches(item)


def test_quarantine_round_trip_keeps_the_cache_in_step(refs, admin):
    item = _live_item(refs, admin)
    movements = StockMovement.objects.count()

    change_part_item_status(item, PartItem.Status.QUARANTINE, by=admin)
    balance = StockBalance.objects.get(batch_line=item.batch_line, location=refs["cell"])
    assert _split(balance) == (Decimal("0"), Decimal("1"))
    assert check_stock_balance() == []

    change_part_item_status(item, PartItem.Status.AVAILABLE, by=admin)
    balance.refresh_from_db()
    assert _split(balance) == (Decimal("1"), Decimal("0"))
    # Quarantine is a status, not a physical move: no fabricated movement.
    assert StockMovement.objects.count() == movements
    _assert_ledger_matches(item)


def test_forbidden_or_unknown_status_is_refused(client, refs, admin):
    item = _live_item(refs, admin)
    with pytest.raises(InventoryError):
        change_part_item_status(item, PartItem.Status.RECEIVING, by=admin)
    client.force_login(admin)
    response = client.post(
        reverse("item_status_change", args=[item.pk]), {"status": "sold"}, follow=True
    )
    assert "Недопустимый переход статуса" in response.content.decode()
    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE


def test_crafted_post_still_receives_canonically_even_without_the_button(
    client, refs, admin, storekeeper
):
    """The UI hides the duplicate button, but the service is what enforces safety."""
    item = _receiving_item(refs, admin)
    client.force_login(storekeeper)

    response = client.post(
        reverse("item_status_change", args=[item.pk]), {"status": "available"}, follow=True
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert StockMovement.objects.filter(part_item=item, movement_type=RECEIPT).count() == 1
    _assert_ledger_matches(item)


def test_canonical_accept_button_still_works_and_picks_a_location(client, refs, admin):
    item = _receiving_item(refs, admin, located=False)
    client.force_login(admin)

    client.post(reverse("item_receive", args=[item.pk]), {"to_location": refs["other"].pk})

    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert item.current_location == refs["other"]
    assert StockMovement.objects.filter(part_item=item, movement_type=RECEIPT).count() == 1
    _assert_ledger_matches(item)
