"""Django admin must not be a second stock-management system.

PartItemAdmin/StockLotAdmin left status/current_location (PartItem) and
quantity/status/location (StockLot) as ordinary editable model fields - a
superuser could rewrite live warehouse state through /admin/ with no
movement, no lock, and no balance refresh, exactly the bypass the dedicated
services and views now guard against. Admin creation was also open for both
models even though the only correct way to create either is the canonical
service (create_part_items / create_stock_lot), which assigns fields the add
form cannot (PartItem.internal_number from NumberSequence; StockLot's
batch-line-quantity accounting) - so admin add is disabled for both.
"""
from decimal import Decimal

import pytest
from django.contrib import admin as dj_admin
from django.urls import reverse

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.admin import PartItemAdmin, StockLotAdmin
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import (
    check_stock_balance,
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="stock-admin", password=PASSWORD)


@pytest.fixture
def refs(db):
    bulk = PartType.objects.create(
        name="Болт", category=Category.objects.create(name="Крепёж"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    serial = PartType.objects.create(
        name="Насос", category=Category.objects.get(name="Крепёж"),
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.SERIAL,
    )
    cell = StorageLocation.objects.create(
        name="Ячейка A", code="S03-D01-C01", storage_allowed=True, is_active=True
    )
    other = StorageLocation.objects.create(
        name="Ячейка B", code="S03-D01-C02", storage_allowed=True, is_active=True
    )
    return {"sup": Supplier.objects.create(name="Поставщик"), "bulk": bulk, "serial": serial,
            "cell": cell, "other": other}


def _line(refs, admin, *, part, quantity):
    batch = Batch.objects.create(supplier=refs["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(quantity), unit_cost_currency=Decimal("50"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


def _live_item(refs, admin):
    line = _line(refs, admin, part=refs["serial"], quantity="1")
    item = create_part_items(line, 1, current_location=refs["cell"])[0]
    return receive_part_item(item, by=admin)


def _live_lot(refs, admin):
    line = _line(refs, admin, part=refs["bulk"], quantity="10")
    lot = create_stock_lot(line, refs["cell"], Decimal("10"))
    return receive_stock_lot(lot, by=admin)


# --- A/D. PartItem admin: warehouse fields readonly, add disabled ------------


def test_partitem_admin_warehouse_fields_are_not_editable():
    admin_site = PartItemAdmin(PartItem, dj_admin.site)
    assert "status" in admin_site.readonly_fields
    assert "current_location" in admin_site.readonly_fields
    assert admin_site.has_add_permission(None) is False


def test_partitem_admin_change_page_cannot_move_or_change_status(client, refs, admin):
    item = _live_item(refs, admin)
    client.force_login(admin)

    response = client.post(
        reverse("admin:inventory_partitem_change", args=[item.pk]),
        {
            "internal_number": item.internal_number,
            "part_type": item.part_type_id,
            "batch": item.batch_id,
            "batch_line": item.batch_line_id,
            "serial_number": item.serial_number,
            "landed_cost_rub": item.landed_cost_rub,
            "status": PartItem.Status.WRITTEN_OFF,
            "current_location": refs["other"].pk,
            "note": "взлом через админку",
        },
        follow=True,
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert item.current_location == refs["cell"]
    balance = StockBalance.objects.get(batch_line=item.batch_line, location=refs["cell"])
    assert balance.quantity_available == Decimal("1")
    assert check_stock_balance() == []


def test_partitem_admin_add_is_unreachable(client, admin):
    client.force_login(admin)
    response = client.get(reverse("admin:inventory_partitem_add"))
    assert response.status_code == 403


# --- B/D. StockLot admin: warehouse fields readonly, add disabled -----------


def test_stocklot_admin_warehouse_fields_are_not_editable():
    admin_site = StockLotAdmin(StockLot, dj_admin.site)
    assert "quantity" in admin_site.readonly_fields
    assert "status" in admin_site.readonly_fields
    assert "location" in admin_site.readonly_fields
    assert admin_site.has_add_permission(None) is False
    assert admin_site.has_delete_permission(None) is False


def test_stocklot_admin_change_page_cannot_rewrite_quantity_status_or_location(
    client, refs, admin
):
    lot = _live_lot(refs, admin)
    client.force_login(admin)

    response = client.post(
        reverse("admin:inventory_stocklot_change", args=[lot.pk]),
        {
            "part_type": lot.part_type_id,
            "batch": lot.batch_id,
            "batch_line": lot.batch_line_id,
            "initial_quantity": lot.initial_quantity,
            "landed_unit_cost_rub": lot.landed_unit_cost_rub,
            "quantity": "999",
            "status": StockLot.Status.DEPLETED,
            "location": refs["other"].pk,
            "note": "взлом через админку",
        },
        follow=True,
    )

    assert response.status_code == 200
    lot.refresh_from_db()
    assert lot.quantity == Decimal("10")
    assert lot.status == StockLot.Status.AVAILABLE
    assert lot.location == refs["cell"]
    balance = StockBalance.objects.get(batch_line=lot.batch_line, location=refs["cell"])
    assert balance.quantity_available == Decimal("10")
    assert check_stock_balance() == []


def test_stocklot_admin_add_is_unreachable(client, admin):
    client.force_login(admin)
    response = client.get(reverse("admin:inventory_stocklot_add"))
    assert response.status_code == 403


@pytest.mark.parametrize("kind", ["legacy_pending", "transfer_target"])
def test_stocklot_admin_cannot_delete_unjournaled_or_transfer_lot(
    client, refs, admin, kind
):
    from apps.inventory.services import perform_stock_transfer

    source = _live_lot(refs, admin)
    if kind == "legacy_pending":
        # A pending draft lot has no movement but occupies lifetime receipt
        # capacity. Admin deletion must not silently reopen that capacity.
        pending_line = _line(refs, admin, part=refs["bulk"], quantity="11")
        lot = create_stock_lot(pending_line, refs["other"], Decimal("1"))
    else:
        perform_stock_transfer(
            part=source.part_type,
            from_location=refs["cell"],
            to_location=refs["other"],
            quantity="2",
            stock_state=StockLot.Status.AVAILABLE,
            token="admin-delete-integrity",
        )
        lot = StockLot.objects.get(batch_line=source.batch_line, location=refs["other"])
    before_quantity = lot.quantity
    before_moves = StockMovement.objects.filter(stock_lot=lot).count()
    balance_before = check_stock_balance()
    client.force_login(admin)

    response = client.post(
        reverse("admin:inventory_stocklot_delete", args=[lot.pk]),
        {"post": "yes"},
        follow=True,
    )

    assert response.status_code == 403
    assert StockLot.objects.filter(pk=lot.pk).exists()
    lot.refresh_from_db()
    assert lot.quantity == before_quantity
    assert StockMovement.objects.filter(stock_lot=lot).count() == before_moves
    assert check_stock_balance() == balance_before


# --- C. Movement journal itself stays untouched by any of the above ---------


def test_no_movement_was_fabricated_by_the_admin_attempts(refs, admin):
    item = _live_item(refs, admin)
    lot = _live_lot(refs, admin)
    before = StockMovement.objects.count()

    from django.test import Client

    client = Client()
    client.force_login(admin)
    client.post(
        reverse("admin:inventory_partitem_change", args=[item.pk]),
        {
            "internal_number": item.internal_number, "part_type": item.part_type_id,
            "batch": item.batch_id, "batch_line": item.batch_line_id,
            "serial_number": item.serial_number, "landed_cost_rub": item.landed_cost_rub,
            "status": PartItem.Status.SOLD, "current_location": refs["other"].pk, "note": "",
        },
    )
    client.post(
        reverse("admin:inventory_stocklot_change", args=[lot.pk]),
        {
            "part_type": lot.part_type_id, "batch": lot.batch_id, "batch_line": lot.batch_line_id,
            "initial_quantity": lot.initial_quantity,
            "landed_unit_cost_rub": lot.landed_unit_cost_rub,
            "quantity": "1", "status": StockLot.Status.DEPLETED,
            "location": refs["other"].pk, "note": "",
        },
    )

    assert StockMovement.objects.count() == before
