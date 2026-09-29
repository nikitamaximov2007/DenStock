"""A lot's quantity and cell change only through the movement journal.

DS-AUDIT-001: «Изменить» on a lot used to rewrite quantity and cell directly,
even after the lot had been received and sold from: +7 phantom units, no
movement, and a balance cache that still showed the old number. Direct editing
now exists only for a fresh RECEIVING lot with no movements.

DS-AUDIT-006: the generic status buttons used to make a RECEIVING lot
«Доступен» without the receipt movement, and quarantine changes left the
balance cache stale. Leaving receiving now always goes through
receive_stock_lot, and every status change refreshes the cache.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db.models import Q, Sum
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import PartPreferredLocation, StockBalance, StockLot, StockMovement
from apps.inventory.services import (
    LOT_PHYSICAL_STATUSES,
    InventoryError,
    adjust_stock_lot_quantity,
    change_stock_lot_status,
    check_stock_balance,
    create_stock_lot,
    lot_is_directly_editable,
    move_stock_lot,
    receive_stock_lot,
    sell_stock_lot,
    update_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
RECEIPT = StockMovement.MovementType.RECEIVE_LOT


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="lot-admin", password=PASSWORD)


@pytest.fixture
def storekeeper(db, django_user_model):
    user = django_user_model.objects.create_user(username="lot-keeper", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    return user


@pytest.fixture
def refs(db):
    part = PartType.objects.create(
        name="Болт", category=Category.objects.create(name="Крепёж"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    cell = StorageLocation.objects.create(
        name="Ячейка A", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    other = StorageLocation.objects.create(
        name="Ячейка B", code="S01-D01-C02", storage_allowed=True, is_active=True
    )
    return {"sup": Supplier.objects.create(name="Поставщик"), "part": part,
            "cell": cell, "other": other}


def _line(refs, admin, quantity="10"):
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


def _receiving_lot(refs, admin, quantity="10"):
    return create_stock_lot(_line(refs, admin, quantity), refs["cell"], Decimal(quantity))


def _live_lot(refs, admin, quantity="10"):
    return receive_stock_lot(_receiving_lot(refs, admin, quantity), by=admin)


def _ledger_net(batch_line, location) -> Decimal:
    """Net lot quantity the movement journal puts into one cell of one batch line."""
    lots = StockMovement.objects.filter(batch_line=batch_line, stock_lot__isnull=False)
    into = lots.filter(to_location=location).aggregate(s=Sum("quantity"))["s"] or Decimal("0")
    out = lots.filter(from_location=location).aggregate(s=Sum("quantity"))["s"] or Decimal("0")
    return into - out


def _split(balance):
    return balance.quantity_available, balance.quantity_quarantine


def _assert_ledger_matches(lot):
    """Lot state = movement journal = balance cache, for every cell of the line."""
    cells = set(
        StockLot.objects.filter(batch_line=lot.batch_line).values_list("location", flat=True)
    ) | set(
        StockMovement.objects.filter(batch_line=lot.batch_line)
        .filter(Q(from_location__isnull=False) | Q(to_location__isnull=False))
        .values_list("to_location", flat=True)
    )
    for location in StorageLocation.objects.filter(pk__in={c for c in cells if c}):
        live = StockLot.objects.filter(
            batch_line=lot.batch_line, location=location,
            status__in=[s for s in LOT_PHYSICAL_STATUSES if s != StockLot.Status.RECEIVING],
        ).aggregate(s=Sum("quantity"))["s"] or Decimal("0")
        assert live == _ledger_net(lot.batch_line, location), location.code
    assert check_stock_balance() == []


# --- DS-AUDIT-001: direct edit only before the first movement ------------------------


def test_fresh_receiving_lot_without_movements_can_still_be_edited(refs, admin):
    lot = _receiving_lot(refs, admin)
    assert lot_is_directly_editable(lot)

    update_stock_lot(lot, location=refs["other"], quantity=Decimal("8"), note="пересчёт")

    lot.refresh_from_db()
    assert (lot.quantity, lot.location, lot.note) == (Decimal("8"), refs["other"], "пересчёт")
    assert not StockMovement.objects.filter(stock_lot=lot).exists()


def test_received_lot_quantity_edit_is_refused(refs, admin):
    lot = _live_lot(refs, admin)
    receipt = StockMovement.objects.get(stock_lot=lot, movement_type=RECEIPT)

    with pytest.raises(InventoryError, match="Корректировкой"):
        update_stock_lot(lot, location=refs["cell"], quantity=Decimal("12"))

    lot.refresh_from_db()
    receipt.refresh_from_db()
    assert lot.quantity == Decimal("10")
    assert receipt.quantity == Decimal("10")
    assert StockMovement.objects.filter(stock_lot=lot).count() == 1
    _assert_ledger_matches(lot)


def test_sold_from_lot_cannot_be_edited_back_to_phantom_stock(refs, admin):
    lot = _live_lot(refs, admin)
    sell_stock_lot(lot, Decimal("7"), by=admin)
    lot.refresh_from_db()
    assert lot.quantity == Decimal("3")

    with pytest.raises(InventoryError):
        update_stock_lot(lot, location=refs["cell"], quantity=Decimal("10"))

    lot.refresh_from_db()
    assert lot.quantity == Decimal("3")
    balance = StockBalance.objects.get(batch_line=lot.batch_line, location=refs["cell"])
    assert balance.quantity_available == Decimal("3")
    _assert_ledger_matches(lot)


def test_live_lot_cannot_teleport_to_another_cell_by_edit(refs, admin):
    lot = _live_lot(refs, admin)

    with pytest.raises(InventoryError, match="Переместить"):
        update_stock_lot(lot, location=refs["other"], quantity=lot.quantity)

    lot.refresh_from_db()
    assert lot.location == refs["cell"]
    # The canonical transfer still works and leaves a movement.
    move_stock_lot(lot, refs["other"], by=admin)
    lot.refresh_from_db()
    assert lot.location == refs["other"]
    assert StockMovement.objects.filter(
        stock_lot=lot, movement_type=StockMovement.MovementType.MOVE_LOT
    ).exists()
    _assert_ledger_matches(lot)


def test_receiving_lot_with_a_movement_is_no_longer_directly_editable(refs, admin):
    lot = _receiving_lot(refs, admin)
    adjust_stock_lot_quantity(lot, Decimal("-2"), by=admin, comment="брак при разгрузке")
    lot.refresh_from_db()

    assert not lot_is_directly_editable(lot)
    with pytest.raises(InventoryError):
        update_stock_lot(lot, location=refs["cell"], quantity=Decimal("10"))
    lot.refresh_from_db()
    assert lot.quantity == Decimal("8")


def test_note_of_a_live_lot_can_change_without_touching_stock(refs, admin):
    lot = _live_lot(refs, admin)

    update_stock_lot(lot, location=refs["cell"], quantity=lot.quantity, note="верхняя полка")

    lot.refresh_from_db()
    assert (lot.quantity, lot.note) == (Decimal("10"), "верхняя полка")
    _assert_ledger_matches(lot)


def test_crafted_edit_post_is_refused_and_edit_link_is_hidden(client, refs, admin, storekeeper):
    lot = _live_lot(refs, admin)
    sell_stock_lot(lot, Decimal("7"), by=admin)
    client.force_login(storekeeper)

    detail = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert reverse("lot_edit", args=[lot.pk]) not in detail
    assert reverse("lot_adjust", args=[lot.pk]) in detail
    assert reverse("lot_move", args=[lot.pk]) in detail

    form = client.get(reverse("lot_edit", args=[lot.pk]))
    assert form.status_code == 302
    response = client.post(
        reverse("lot_edit", args=[lot.pk]),
        {"location": refs["other"].pk, "quantity": "10", "note": ""},
        follow=True,
    )
    assert "Корректировкой" in response.content.decode()
    lot.refresh_from_db()
    assert (lot.quantity, lot.location) == (Decimal("3"), refs["cell"])
    _assert_ledger_matches(lot)


def test_edit_link_stays_for_a_fresh_receiving_lot(client, refs, admin, storekeeper):
    lot = _receiving_lot(refs, admin)
    client.force_login(storekeeper)
    detail = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert reverse("lot_edit", args=[lot.pk]) in detail
    assert client.get(reverse("lot_edit", args=[lot.pk])).status_code == 200


# --- DS-AUDIT-006: status changes use the canonical receipt and refresh the cache ---


def test_receiving_to_available_writes_one_receipt_and_the_cache(client, refs, admin, storekeeper):
    lot = _receiving_lot(refs, admin)
    client.force_login(storekeeper)

    client.post(reverse("lot_status_change", args=[lot.pk]), {"status": "available"})
    client.post(reverse("lot_status_change", args=[lot.pk]), {"status": "available"})

    lot.refresh_from_db()
    assert lot.status == StockLot.Status.AVAILABLE
    assert StockMovement.objects.filter(stock_lot=lot, movement_type=RECEIPT).count() == 1
    balance = StockBalance.objects.get(batch_line=lot.batch_line, location=refs["cell"])
    assert balance.quantity_available == Decimal("10")
    assert PartPreferredLocation.objects.get(part_type=refs["part"]).location == refs["cell"]
    _assert_ledger_matches(lot)


def test_receiving_lot_offers_accept_not_a_second_available_button(client, refs, admin):
    lot = _receiving_lot(refs, admin)
    client.force_login(admin)
    detail = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert reverse("lot_receive", args=[lot.pk]) in detail
    assert 'name="status" value="available"' not in detail
    assert 'name="status" value="quarantine"' in detail


def test_receiving_to_quarantine_is_received_first(refs, admin):
    lot = _receiving_lot(refs, admin)

    change_stock_lot_status(lot, StockLot.Status.QUARANTINE, by=admin)

    lot.refresh_from_db()
    assert lot.status == StockLot.Status.QUARANTINE
    assert StockMovement.objects.filter(stock_lot=lot, movement_type=RECEIPT).count() == 1
    balance = StockBalance.objects.get(batch_line=lot.batch_line, location=refs["cell"])
    assert _split(balance) == (Decimal("0"), Decimal("10"))
    _assert_ledger_matches(lot)


def test_quarantine_round_trip_keeps_the_cache_in_step(refs, admin):
    lot = _live_lot(refs, admin)
    movements = StockMovement.objects.count()

    change_stock_lot_status(lot, StockLot.Status.QUARANTINE, by=admin)
    balance = StockBalance.objects.get(batch_line=lot.batch_line, location=refs["cell"])
    assert _split(balance) == (Decimal("0"), Decimal("10"))
    assert check_stock_balance() == []

    change_stock_lot_status(lot, StockLot.Status.AVAILABLE, by=admin)
    balance.refresh_from_db()
    assert _split(balance) == (Decimal("10"), Decimal("0"))
    # Quarantine is a status, not a physical move: no fabricated movement.
    assert StockMovement.objects.count() == movements
    _assert_ledger_matches(lot)


def test_forbidden_or_unknown_status_is_refused(client, refs, admin):
    lot = _live_lot(refs, admin)
    with pytest.raises(InventoryError):
        change_stock_lot_status(lot, StockLot.Status.RECEIVING, by=admin)
    client.force_login(admin)
    response = client.post(
        reverse("lot_status_change", args=[lot.pk]), {"status": "depleted"}, follow=True
    )
    assert "Недопустимый переход статуса" in response.content.decode()
    lot.refresh_from_db()
    assert lot.status == StockLot.Status.AVAILABLE
