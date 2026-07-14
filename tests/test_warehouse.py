from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse

from apps.accounts import roles
from apps.actions.models import WarehouseAction
from apps.catalog.models import Category, PartType, Unit
from apps.core.scanner import resolve_scan
from apps.inventory.models import StockBalance, StockMovement
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.forms import StorageLocationForm
from apps.warehouse.models import StorageLocation, StorageLocationRenameHistory
from apps.warehouse.services import StorageLocationRenameError, rename_storage_location

PASSWORD = "parol-12345"
L = StorageLocation.Level
P = StorageLocation.Purpose


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


def _create_payload(**over):
    data = {
        "name": "Место",
        "code": "LOC-1",
        "level": L.CELL,
        "purpose": P.NORMAL,
        "sort_order": 0,
        "storage_allowed": "on",
    }
    data.update(over)
    return data


def test_create_warehouse(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    resp = client.post(
        reverse("location_create"),
        _create_payload(name="Основной склад", code="СКЛАД-1", level=L.WAREHOUSE),
    )
    assert resp.status_code == 302
    assert StorageLocation.objects.filter(code="СКЛАД-1", level=L.WAREHOUSE).exists()


def test_create_zone_inside_warehouse(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    wh = StorageLocation.objects.create(name="Склад", code="СКЛАД-1", level=L.WAREHOUSE)
    resp = client.post(
        reverse("location_create"),
        _create_payload(name="Зона A", code="A", level=L.ZONE, parent=wh.pk),
    )
    assert resp.status_code == 302
    zone = StorageLocation.objects.get(code="A")
    assert zone.parent == wh


def test_create_nested_levels(db):
    wh = StorageLocation.objects.create(name="Склад", code="СКЛАД-1", level=L.WAREHOUSE)
    zone = StorageLocation.objects.create(name="A", code="A", level=L.ZONE, parent=wh)
    rack = StorageLocation.objects.create(name="03", code="03", level=L.RACK, parent=zone)
    shelf = StorageLocation.objects.create(name="02", code="02", level=L.SHELF, parent=rack)
    cell = StorageLocation.objects.create(name="04", code="04", level=L.CELL, parent=shelf)
    assert cell.parent == shelf
    assert rack.level == L.RACK


def test_full_address_built(db):
    wh = StorageLocation.objects.create(name="Склад", code="СКЛАД-1", level=L.WAREHOUSE)
    zone = StorageLocation.objects.create(name="A", code="A", level=L.ZONE, parent=wh)
    cell = StorageLocation.objects.create(name="03", code="03", level=L.CELL, parent=zone)
    assert cell.full_path == "СКЛАД-1 / A / 03"


def test_barcode_generated(db):
    loc = StorageLocation.objects.create(name="Ячейка", code="A-03-02", level=L.CELL)
    assert loc.barcode == "LOC:A-03-02"


def test_code_is_unique(db):
    StorageLocation.objects.create(name="Первая", code="DUP", level=L.CELL)
    dup = StorageLocation(name="Вторая", code="DUP", level=L.CELL, purpose=P.NORMAL)
    with pytest.raises(ValidationError):
        dup.full_clean()


def test_parent_cycle_rejected(db):
    a = StorageLocation.objects.create(name="A", code="A", level=L.ZONE)
    b = StorageLocation.objects.create(name="B", code="B", level=L.RACK, parent=a)
    a.parent = b
    with pytest.raises(ValidationError):
        a.full_clean()


def test_toggle_deactivates_without_delete(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    loc = StorageLocation.objects.create(name="Ячейка", code="C1", level=L.CELL)
    resp = client.post(reverse("location_toggle", args=[loc.pk]))
    assert resp.status_code == 302
    loc.refresh_from_db()
    assert loc.is_active is False
    assert StorageLocation.objects.filter(pk=loc.pk).exists()


def test_purpose_independent_of_level(db):
    zone = StorageLocation.objects.create(
        name="Приёмка", code="RCV", level=L.ZONE, purpose=P.RECEIVING
    )
    cell = StorageLocation.objects.create(
        name="Карантин", code="QRN", level=L.CELL, purpose=P.QUARANTINE
    )
    assert zone.level == L.ZONE and zone.purpose == P.RECEIVING
    assert cell.level == L.CELL and cell.purpose == P.QUARANTINE


def test_cannot_deactivate_with_active_children(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    wh = StorageLocation.objects.create(name="Склад", code="СКЛАД-1", level=L.WAREHOUSE)
    StorageLocation.objects.create(name="A", code="A", level=L.ZONE, parent=wh)
    resp = client.post(reverse("location_toggle", args=[wh.pk]))
    assert resp.status_code == 302
    wh.refresh_from_db()
    assert wh.is_active is True  # деактивация заблокирована


def test_storekeeper_cannot_create(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("location_create"), _create_payload(code="X1"))
    assert resp.status_code == 403
    assert not StorageLocation.objects.filter(code="X1").exists()


def test_viewer_cannot_edit(make_user, client):
    make_user("nabl", role=roles.VIEWER)
    client.login(username="nabl", password=PASSWORD)
    loc = StorageLocation.objects.create(name="Ячейка", code="C9", level=L.CELL)
    resp = client.post(reverse("location_edit", args=[loc.pk]), _create_payload(code="C9"))
    assert resp.status_code == 403


def test_storekeeper_can_view(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    loc = StorageLocation.objects.create(name="Ячейка", code="C2", level=L.CELL)
    assert client.get(reverse("warehouse_index")).status_code == 200
    assert client.get(reverse("location_detail", args=[loc.pk])).status_code == 200


def test_navigation_shows_warehouse(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("dashboard")).content.decode()
    assert "Склад" in html


def _finalized_line(supplier, part, admin, *, quantity):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def renamed_inventory(db, make_user):
    admin = make_user("warehouse-admin", is_superuser=True)
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Расходники")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Рабочая ячейка",
        code="S04-L03-D01-C04",
        storage_allowed=True,
        is_active=True,
    )
    bulk_part = PartType.objects.create(
        name="Втулка",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    serial_part = PartType.objects.create(
        name="Датчик",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
    )
    bulk_line = _finalized_line(supplier, bulk_part, admin, quantity="5")
    serial_line = _finalized_line(supplier, serial_part, admin, quantity="1")
    lot = create_stock_lot(bulk_line, location, Decimal("5"))
    receive_stock_lot(lot, by=admin)
    lot.refresh_from_db()
    item = create_part_items(serial_line, 1)[0]
    receive_part_item(item, to_location=location, by=admin)
    item.refresh_from_db()
    action = WarehouseAction.objects.create(
        action_type=WarehouseAction.Type.SALE,
        part_type=bulk_part,
        part_number="420931285",
        part_name="Втулка",
        location=location,
        location_code=location.code,
        quantity=Decimal("1"),
        customer_comment="Снимок ячейки",
    )
    return {
        "admin": admin,
        "location": location,
        "lot": lot,
        "item": item,
        "action": action,
        "bulk_line": bulk_line,
        "serial_line": serial_line,
    }


def test_rename_keeps_location_identity_stock_and_action_snapshot(renamed_inventory, client):
    data = renamed_inventory
    location = data["location"]
    old_code = location.code
    original_barcode = location.barcode
    lot = data["lot"]
    item = data["item"]
    action = data["action"]
    balance = StockBalance.objects.get(batch_line=data["bulk_line"], location=location)
    movements_before = StockMovement.objects.count()
    lot_before = (
        lot.location_id,
        lot.quantity,
        lot.initial_quantity,
        lot.status,
        lot.landed_unit_cost_rub,
        lot.batch_id,
        lot.batch_line_id,
        lot.part_type_id,
    )
    balance_before = (
        balance.location_id,
        balance.quantity_physical,
        balance.quantity_available,
        balance.quantity_reserved,
        balance.quantity_quarantine,
        balance.quantity_in_repair,
        balance.batch_id,
        balance.batch_line_id,
        balance.part_type_id,
    )
    item_before = (
        item.current_location_id,
        item.status,
        item.landed_cost_rub,
        item.batch_id,
        item.batch_line_id,
        item.part_type_id,
    )

    renamed = rename_storage_location(
        location,
        new_code=" s04-l03-d01-c05 ",
        expected_code=old_code,
        by=data["admin"],
    )

    assert renamed.pk == location.pk
    assert renamed.code == "S04-L03-D01-C05"
    location.refresh_from_db()
    lot.refresh_from_db()
    balance.refresh_from_db()
    item.refresh_from_db()
    action.refresh_from_db()
    assert location.barcode == original_barcode
    assert lot_before == (
        lot.location_id,
        lot.quantity,
        lot.initial_quantity,
        lot.status,
        lot.landed_unit_cost_rub,
        lot.batch_id,
        lot.batch_line_id,
        lot.part_type_id,
    )
    assert balance_before == (
        balance.location_id,
        balance.quantity_physical,
        balance.quantity_available,
        balance.quantity_reserved,
        balance.quantity_quarantine,
        balance.quantity_in_repair,
        balance.batch_id,
        balance.batch_line_id,
        balance.part_type_id,
    )
    assert item_before == (
        item.current_location_id,
        item.status,
        item.landed_cost_rub,
        item.batch_id,
        item.batch_line_id,
        item.part_type_id,
    )
    assert StockMovement.objects.count() == movements_before
    assert action.location_id == location.pk
    assert action.location.code == "S04-L03-D01-C05"
    assert action.location_code == old_code
    assert action.part_number == "420931285"
    assert StorageLocationRenameHistory.objects.filter(
        location=location,
        old_code=old_code,
        new_code="S04-L03-D01-C05",
        renamed_by=data["admin"],
    ).count() == 1

    assert resolve_scan(old_code).status == "unknown"
    assert resolve_scan(location.code).id == location.pk
    # Штрихкод не является алиасом кода и сохраняется, потому что меняется только code.
    assert resolve_scan(original_barcode).id == location.pk
    client.force_login(data["admin"])
    label = client.get(reverse("label_location", args=[location.pk]))
    assert label.status_code == 200
    assert location.code in label.content.decode()


@pytest.mark.parametrize(
    ("new_code", "message"),
    [
        ("S04 L03", "без пробелов"),
        ("420931285", "Номер детали"),
        (" s04-l03-d01-c04 ", "совпадает"),
    ],
)
def test_rename_rejects_invalid_or_unchanged_code(renamed_inventory, new_code, message):
    location = renamed_inventory["location"]
    with pytest.raises(StorageLocationRenameError, match=message):
        rename_storage_location(
            location,
            new_code=new_code,
            expected_code=location.code,
            by=renamed_inventory["admin"],
        )
    location.refresh_from_db()
    assert location.code == "S04-L03-D01-C04"
    assert StorageLocationRenameHistory.objects.count() == 0


def test_rename_rejects_occupied_or_stale_code(renamed_inventory):
    location = renamed_inventory["location"]
    StorageLocation.objects.create(name="Занятая", code="S04-L03-D01-C05")
    with pytest.raises(StorageLocationRenameError, match="уже существует"):
        rename_storage_location(
            location,
            new_code="S04-L03-D01-C05",
            expected_code=location.code,
            by=renamed_inventory["admin"],
        )
    with pytest.raises(StorageLocationRenameError, match="другим пользователем"):
        rename_storage_location(
            location,
            new_code="S04-L03-D01-C06",
            expected_code="S04-L03-D01-C03",
            by=renamed_inventory["admin"],
        )
    assert StorageLocationRenameHistory.objects.count() == 0


def test_rename_converts_concurrent_unique_conflict_to_user_error(renamed_inventory):
    location = renamed_inventory["location"]
    with patch(
        "apps.warehouse.services._persist_location_rename",
        side_effect=IntegrityError,
    ):
        with pytest.raises(StorageLocationRenameError, match="уже существует"):
            rename_storage_location(
                location,
                new_code="S04-L03-D01-C05",
                expected_code=location.code,
                by=renamed_inventory["admin"],
            )
    location.refresh_from_db()
    assert location.code == "S04-L03-D01-C04"
    assert StorageLocationRenameHistory.objects.count() == 0


def test_rename_view_permissions_double_post_and_generic_edit_guard(
    renamed_inventory, make_user, client
):
    location = renamed_inventory["location"]
    rename_url = reverse("location_rename", args=[location.pk])
    viewer = make_user("rename-viewer", role=roles.VIEWER)
    client.force_login(viewer)
    assert client.get(rename_url).status_code == 403
    assert client.post(
        rename_url,
        {"expected_code": location.code, "new_code": "S04-L03-D01-C05"},
    ).status_code == 403

    client.force_login(renamed_inventory["admin"])
    page = client.get(rename_url)
    assert page.status_code == 200
    assert "Текущий код ячейки" in page.content.decode()
    payload = {"expected_code": location.code, "new_code": "S04-L03-D01-C05"}
    assert client.post(rename_url, payload).status_code == 302
    repeated = client.post(rename_url, payload)
    assert repeated.status_code == 200
    assert "уже изменён другим пользователем" in repeated.content.decode()
    assert StorageLocationRenameHistory.objects.filter(location=location).count() == 1

    response = client.post(
        reverse("location_edit", args=[location.pk]),
        _create_payload(name="Новое имя", code="S04-L03-D01-C99"),
    )
    assert response.status_code == 302
    location.refresh_from_db()
    assert location.name == "Новое имя"
    assert location.code == "S04-L03-D01-C05"


def test_existing_location_edit_keeps_code_and_barcode_server_side(renamed_inventory, client):
    location = renamed_inventory["location"]
    original_code = location.code
    original_barcode = location.barcode
    client.force_login(renamed_inventory["admin"])
    edit_url = reverse("location_edit", args=[location.pk])

    page = client.get(edit_url)
    html = page.content.decode()
    assert page.status_code == 200
    assert f'value="{original_code}"' in html
    assert "Код существующей ячейки изменяется через отдельную операцию" in html
    assert reverse("location_rename", args=[location.pk]) in html
    assert f'value="{original_barcode}"' in html

    response = client.post(
        edit_url,
        {
            "name": "Ячейка после правки",
            "code": "S04-L03-D01-C99",
            "barcode": "LOC:S04-L03-D01-C99",
            "level": L.SHELF,
            "purpose": P.QUARANTINE,
            "storage_allowed": "on",
            "sort_order": "7",
            "description": "Проверенная административная настройка",
            "capacity": "18",
        },
    )
    assert response.status_code == 302
    location.refresh_from_db()
    assert location.code == original_code
    assert location.barcode == original_barcode
    assert location.name == "Ячейка после правки"
    assert location.level == L.SHELF
    assert location.purpose == P.QUARANTINE
    assert location.sort_order == 7
    assert location.description == "Проверенная административная настройка"
    assert location.capacity == 18
    assert StorageLocationRenameHistory.objects.filter(location=location).count() == 0

    form = StorageLocationForm(
        instance=location,
        data={
            "name": location.name,
            "code": "S04-L03-D01-C98",
            "barcode": "LOC:S04-L03-D01-C98",
            "level": location.level,
            "purpose": location.purpose,
            "storage_allowed": "on",
            "sort_order": str(location.sort_order),
            "description": location.description,
            "capacity": location.capacity,
        },
    )
    assert "code" not in form.fields
    assert "barcode" not in form.fields
    assert form.is_valid()
    form.save()
    location.refresh_from_db()
    assert location.code == original_code
    assert location.barcode == original_barcode


def test_admin_change_form_keeps_existing_location_identity(renamed_inventory, client):
    location = renamed_inventory["location"]
    admin_user = renamed_inventory["admin"]
    original_code = location.code
    original_barcode = location.barcode
    client.force_login(admin_user)
    admin_url = reverse("admin:warehouse_storagelocation_change", args=[location.pk])

    page = client.get(admin_url)
    assert page.status_code == 200
    assert 'name="code"' not in page.content.decode()
    assert 'name="barcode"' not in page.content.decode()

    response = client.post(
        admin_url,
        {
            "name": "Правка через admin",
            "code": "S04-L03-D01-C99",
            "barcode": "LOC:S04-L03-D01-C99",
            "level": location.level,
            "purpose": location.purpose,
            "storage_allowed": "on",
            "is_active": "on",
            "sort_order": str(location.sort_order),
            "description": location.description,
            "capacity": location.capacity or "",
            "_save": "Save",
        },
    )
    assert response.status_code == 302
    location.refresh_from_db()
    assert location.code == original_code
    assert location.barcode == original_barcode
    assert location.name == "Правка через admin"
    assert StorageLocationRenameHistory.objects.filter(location=location).count() == 0


def test_rename_rejects_external_next_url(renamed_inventory, client):
    location = renamed_inventory["location"]
    client.force_login(renamed_inventory["admin"])
    rename_url = reverse("location_rename", args=[location.pk])
    response = client.post(
        rename_url,
        {
            "expected_code": location.code,
            "new_code": "S04-L03-D01-C05",
            "next": "https://evil.example/",
        },
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("location_detail", args=[location.pk])
