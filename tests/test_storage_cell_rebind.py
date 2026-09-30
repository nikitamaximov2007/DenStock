"""Moving a cell to another address detaches it from the old place and binds it to the new one.

"Переименовать ячейку" used to change only the code of the same row and left
the old code as an ACTIVE alias: the old address kept resolving to the moved
cell in scans, move destinations, receiving and counting, could not be used
for a new cell (``get_or_create_location`` returned the moved one), and the
cell kept its old drawer as parent when D changed. The operation is now a
physical rebind: the cell (with its stock) keeps its identity, is re-parented
under the target drawer, the old code/barcode survive only as a NON-active
historical alias, and the old address is free for a new cell.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.actions.models import WarehouseAction
from apps.catalog.models import Category, PartType, Unit
from apps.core.scanner import resolve_scan
from apps.core.views import _resolve_move_destination
from apps.inventory.models import (
    PartItem,
    PartPreferredLocation,
    StockBalance,
    StockLocationLock,
    StockLot,
    StockMovement,
)
from apps.inventory.services import (
    check_stock_balance,
    create_part_items,
    create_stock_lot,
    move_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import create_location, get_or_create_location
from apps.warehouse.models import (
    StorageLocation,
    StorageLocationAlias,
    StorageLocationRenameHistory,
)
from apps.warehouse.services import (
    StorageLocationRenameError,
    attach_movement_location_history,
    rebind_storage_cell,
    resolve_storage_location,
)

PASSWORD = "parol-12345"


def _line(env, part, quantity):
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(quantity), unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    return line


@pytest.fixture
def env(db, django_user_model):
    admin = django_user_model.objects.create_superuser(username="rebind-admin", password=PASSWORD)
    category = Category.objects.create(name="Перенос")
    unit = Unit.objects.get(name="Штука")
    cell = create_location("S03-D02-C08")
    neighbour = create_location("S03-D02-C09", name="Соседняя")
    env = {"admin": admin, "supplier": Supplier.objects.create(name="Поставщик"),
           "cell": cell, "neighbour": neighbour}
    bulk = PartType.objects.create(
        name="Втулка", category=category, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
    )
    serial = PartType.objects.create(
        name="Датчик", category=category, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL,
    )
    lot = receive_stock_lot(create_stock_lot(_line(env, bulk, "5"), cell, Decimal("5")), by=admin)
    item = create_part_items(_line(env, serial, "1"), 1)[0]
    receive_part_item(item, to_location=cell, by=admin)
    env.update(bulk=bulk, serial=serial, lot=lot, item=PartItem.objects.get(pk=item.pk))
    return env


def _move(env, new_code="S03-D02-C07"):
    return rebind_storage_cell(
        env["cell"], new_code=new_code, expected_code=env["cell"].code, by=env["admin"]
    )


def _snapshot(env):
    lot = StockLot.objects.get(pk=env["lot"].pk)
    item = PartItem.objects.get(pk=env["item"].pk)
    balances = sorted(
        StockBalance.objects.values_list(
            "location_id", "batch_line_id", "quantity_physical", "quantity_available"
        )
    )
    return (lot.location_id, lot.quantity, lot.status, item.current_location_id, item.status,
            balances, StockMovement.objects.count())


# --- A. Basic rebind -------------------------------------------------------------


def test_cell_is_detached_from_the_old_address_and_bound_to_the_new_one(env):
    cell = env["cell"]
    before = _snapshot(env)

    moved = _move(env)

    assert moved.pk == cell.pk
    assert (moved.code, moved.barcode, moved.short_code) == (
        "S03-D02-C07", "LOC:S03-D02-C07", "3-2-7"
    )
    assert moved.parent.code == "S03-D02"
    assert not StorageLocation.objects.filter(code="S03-D02-C08").exists()
    assert StorageLocation.objects.filter(code="S03-D02-C07").count() == 1
    assert _snapshot(env) == before  # no stock moved, no movement booked
    entry = StorageLocationRenameHistory.objects.get(location=cell)
    assert (entry.old_code, entry.new_code, entry.renamed_by) == (
        "S03-D02-C08", "S03-D02-C07", env["admin"]
    )


def test_operator_form_is_accepted_and_stored_canonically(env):
    moved = _move(env, new_code=" 3-2-7 ")
    assert moved.code == "S03-D02-C07"


def test_moving_to_another_drawer_rebinds_the_parent_and_leaves_siblings(env):
    moved = _move(env, new_code="S03-D05-C01")

    assert moved.code == "S03-D05-C01"
    assert moved.parent.code == "S03-D05"
    assert moved.parent.level == StorageLocation.Level.DRAWER
    assert moved.parent.parent.code == "S03"
    env["neighbour"].refresh_from_db()
    assert env["neighbour"].code == "S03-D02-C09"
    assert env["neighbour"].parent.code == "S03-D02"


def test_a_name_that_was_just_the_old_address_follows_the_new_address(env):
    assert env["cell"].name == "S03-D02-C08"
    assert _move(env).name == "S03-D02-C07"
    neighbour = rebind_storage_cell(
        env["neighbour"], new_code="S03-D02-C10", expected_code="S03-D02-C09", by=env["admin"]
    )
    assert neighbour.name == "Соседняя"


# --- B/L. Old address and old label: history only, never a live location -------


def test_old_code_and_barcode_are_not_a_live_location(env):
    _move(env)

    for old in ("S03-D02-C08", "3-2-8", "LOC:S03-D02-C08"):
        assert resolve_storage_location(old) == (None, False)
        scan = resolve_scan(old)
        assert scan.status == "unknown"
        assert scan.id is None
        assert scan.message.startswith("Ячейка перенесена: 3-2-8 -> 3-2-7.")
    destination, error = _resolve_move_destination("S03-D02-C08")
    assert destination is None
    assert "Ячейка перенесена: 3-2-8 -> 3-2-7" in error
    alias = StorageLocationAlias.objects.get(location=env["cell"])
    assert (alias.code, alias.barcode, alias.is_active) == (
        "S03-D02-C08", "LOC:S03-D02-C08", False
    )


def test_new_code_barcode_and_operator_form_resolve_to_the_moved_cell(env):
    _move(env)
    for new in ("S03-D02-C07", "3-2-7", "LOC:S03-D02-C07"):
        scan = resolve_scan(new)
        assert (scan.status, scan.id, scan.is_alias) == ("found", env["cell"].pk, False)


def test_a_custom_label_stays_on_the_physical_cell(env):
    StorageLocation.objects.filter(pk=env["cell"].pk).update(barcode="BOX-42")
    env["cell"].refresh_from_db()
    moved = _move(env)
    assert moved.barcode == "BOX-42"
    assert resolve_scan("BOX-42").id == moved.pk


# --- E (old address). The vacated place is free for a new cell -------------------


def test_the_old_address_can_hold_a_new_empty_cell(env):
    _move(env)

    fresh = create_location("S03-D02-C08", name="Новая ячейка")

    assert fresh.pk != env["cell"].pk
    assert fresh.parent.code == "S03-D02"
    assert resolve_scan("S03-D02-C08").id == fresh.pk
    assert not StockLot.objects.filter(location=fresh).exists()
    assert not PartItem.objects.filter(current_location=fresh).exists()
    assert StockLot.objects.get(pk=env["lot"].pk).location_id == env["cell"].pk
    assert check_stock_balance() == []


# --- D/E/F. Live stock follows the cell, balances stay exact ---------------------


def test_lot_item_and_balances_follow_the_cell_without_phantom_stock(env, client):
    _move(env)

    lot = StockLot.objects.get(pk=env["lot"].pk)
    item = PartItem.objects.get(pk=env["item"].pk)
    assert (lot.location.code, lot.quantity) == ("S03-D02-C07", Decimal("5"))
    assert item.current_location.code == "S03-D02-C07"
    assert item.status == PartItem.Status.AVAILABLE
    balance = StockBalance.objects.get(batch_line=lot.batch_line)
    assert (balance.location_id, balance.quantity_physical) == (env["cell"].pk, Decimal("5"))
    assert not StockBalance.objects.exclude(location=env["cell"]).exists()
    assert check_stock_balance() == []
    client.force_login(env["admin"])
    page = client.get(reverse("location_detail", args=[env["cell"].pk])).content.decode()
    assert "3-2-7" in page and "Втулка" in page

    # the moved stock is still operable at the new address
    move_stock_lot(lot, env["neighbour"], by=env["admin"])
    assert check_stock_balance() == []


# --- G. Collisions fail closed -----------------------------------------------------


def _unchanged(env):
    cell = StorageLocation.objects.get(pk=env["cell"].pk)
    assert (cell.code, cell.barcode, cell.parent.code) == (
        "S03-D02-C08", "LOC:S03-D02-C08", "S03-D02"
    )
    assert not StorageLocationAlias.objects.filter(location=cell).exists()
    assert not StorageLocationRenameHistory.objects.filter(location=cell).exists()


def test_occupied_target_is_refused_without_partial_change(env):
    with pytest.raises(StorageLocationRenameError, match="уже существует"):
        _move(env, new_code="S03-D02-C09")
    _unchanged(env)
    env["neighbour"].refresh_from_db()
    assert env["neighbour"].code == "S03-D02-C09"


def test_an_archived_cell_still_occupies_its_address(env):
    StorageLocation.objects.filter(pk=env["neighbour"].pk).update(is_active=False)
    with pytest.raises(StorageLocationRenameError):
        _move(env, new_code="S03-D02-C09")
    _unchanged(env)


def test_an_address_another_cell_still_answers_to_is_occupied(env):
    other = create_location("S03-D04-C03")
    StorageLocationAlias.objects.create(
        location=other, code="S03-D02-C07", kind=StorageLocationAlias.Kind.DRAWER
    )
    with pytest.raises(StorageLocationRenameError, match="alias"):
        _move(env, new_code="S03-D02-C07")
    _unchanged(env)
    assert resolve_scan("S03-D02-C07").id == other.pk


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("S03-D02-C08", "совпадает"),
        ("3-2-8", "совпадает"),
        ("S03-D02", "а не стеллаж или ящик"),
        ("S03-L02-D02-C07", "S-D-C"),
        ("420931285", "S-D-C"),
        ("", "Укажите"),
    ],
)
def test_invalid_or_same_target_is_refused(env, target, message):
    with pytest.raises(StorageLocationRenameError, match=message):
        _move(env, new_code=target)
    _unchanged(env)


def test_a_cell_under_section_recount_is_not_moved(env):
    StockLocationLock.objects.create(
        location=env["cell"], section_code="S03-D02", document_id=1
    )
    with pytest.raises(StorageLocationRenameError, match="пересчёте"):
        _move(env)
    _unchanged(env)


def test_a_failure_mid_rebind_rolls_everything_back(env):
    with patch(
        "apps.warehouse.services.StorageLocationRenameHistory.objects.create",
        side_effect=RuntimeError("сбой"),
    ):
        with pytest.raises(RuntimeError):
            _move(env)
    _unchanged(env)


# --- H/I. Server side: the view only runs the canonical service ------------------


def test_view_previews_then_rebinds_and_a_repeat_is_refused(client, env):
    client.force_login(env["admin"])
    url = reverse("location_rename", args=[env["cell"].pk])
    payload = {"expected_code": "S03-D02-C08", "new_code": "3-2-7"}

    preview = client.post(url, payload)
    html = preview.content.decode()
    assert preview.status_code == 200
    assert "Подтвердить перенос" in html
    assert "3-2-8" in html and "3-2-7" in html
    assert "старый адрес освободится" in html
    _unchanged(env)

    done = client.post(url, {**payload, "new_code": "S03-D02-C07", "confirm": "1"}, follow=True)
    assert "Ячейка перенесена: 3-2-8 -&gt; 3-2-7" in done.content.decode()
    assert StorageLocation.objects.get(pk=env["cell"].pk).code == "S03-D02-C07"

    again = client.post(url, {**payload, "confirm": "1"})
    assert again.status_code == 200
    assert "уже изменён другим пользователем" in again.content.decode()
    assert StorageLocationRenameHistory.objects.filter(location=env["cell"]).count() == 1


def test_crafted_post_cannot_store_a_non_s_d_c_code(client, env):
    client.force_login(env["admin"])
    url = reverse("location_rename", args=[env["cell"].pk])

    response = client.post(
        url, {"expected_code": "S03-D02-C08", "new_code": "ANY-LABEL", "confirm": "1"}
    )

    assert response.status_code == 200
    _unchanged(env)


def test_the_page_describes_a_move_not_a_label_change(client, env):
    client.force_login(env["admin"])
    html = client.get(reverse("location_rename", args=[env["cell"].pk])).content.decode()
    assert "Перенести ячейку на другой адрес" in html
    assert "адрес освобождается" in html
    assert "Изменится обозначение" not in html
    detail = client.get(reverse("location_detail", args=[env["cell"].pk])).content.decode()
    assert "Перенести ячейку" in detail


# --- K. History stays auditable ----------------------------------------------------


def test_history_keeps_the_address_at_the_time_of_each_event(env):
    action = WarehouseAction.objects.create(
        action_type=WarehouseAction.Type.SALE, part_type=env["bulk"], part_number="X-1",
        part_name="Втулка", location=env["cell"], location_code=env["cell"].code,
        quantity=Decimal("1"), customer_comment="снимок",
    )
    _move(env)

    movements = list(StockMovement.objects.filter(stock_lot=env["lot"]))
    attach_movement_location_history(movements)
    receipt = movements[0]
    assert receipt.to_location_historical_code == "S03-D02-C08"
    assert receipt.to_location.code == "S03-D02-C07"
    assert receipt.to_location_was_renamed is True
    action.refresh_from_db()
    assert action.location_code == "S03-D02-C08"
    assert action.location_id == env["cell"].pk


# --- M. Preferred location follows the physical cell ---------------------------------


def test_preferred_location_follows_the_physical_cell(client, env):
    preference = PartPreferredLocation.objects.get(part_type=env["bulk"])
    assert preference.location_id == env["cell"].pk

    _move(env)

    preference.refresh_from_db()
    assert preference.location_id == env["cell"].pk
    assert preference.location.code == "S03-D02-C07"
    client.force_login(env["admin"])
    guidance = client.get(
        reverse("scanner_receiving_location_guidance"), {"part": env["bulk"].pk}
    ).json()
    assert guidance["location"]["id"] == env["cell"].pk


def test_counting_at_the_vacated_address_counts_a_new_cell_not_the_moved_one(env):
    _move(env)
    cell = get_or_create_location("S03-D02-C08")
    assert cell.pk != env["cell"].pk
    assert not StockLot.objects.filter(location=cell).exists()


def test_admin_cannot_rebind_a_cell_by_editing_its_parent(client, env):
    other_drawer = create_location("S03-D05")
    client.force_login(env["admin"])
    url = reverse("admin:warehouse_storagelocation_change", args=[env["cell"].pk])
    assert 'name="parent"' not in client.get(url).content.decode()

    client.post(url, {
        "name": "Правка", "parent": other_drawer.pk, "level": env["cell"].level,
        "purpose": env["cell"].purpose, "storage_allowed": "on", "is_active": "on",
        "sort_order": "0", "description": "", "capacity": "", "_save": "Save",
    })

    cell = StorageLocation.objects.get(pk=env["cell"].pk)
    assert cell.parent.code == "S03-D02"
    assert cell.code == "S03-D02-C08"
