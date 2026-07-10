"""Слой 10 — журнал движений (StockMovement) и кэш остатков (StockBalance).

Покрывает обязательные проверки плана 10-layer-10-stock-movement-balance.md §15.
"""
from decimal import Decimal

import pytest
from django.contrib import admin as dj_admin
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts import roles
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse as promote_brp
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.admin import StockBalanceAdmin, StockMovementAdmin
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    adjust_stock_lot_quantity,
    backfill_opening_movements,
    check_stock_balance,
    create_part_items,
    create_stock_lot,
    move_part_item,
    move_stock_lot,
    rebuild_stock_balance,
    receive_part_item,
    receive_stock_lot,
)
from apps.polaris.models import PolarisCatalogPart
from apps.polaris.services import promote_to_warehouse as promote_polaris
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


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


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


@pytest.fixture
def refs(db):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Крепёж")
    unit = Unit.objects.get(name="Штука")
    bulk = PartType.objects.create(
        name="Болт", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    serial = PartType.objects.create(
        name="Насос", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    PartNumber.objects.create(
        part=bulk, value="BOLT-001", kind=PartNumber.Kind.OEM, is_primary=True
    )
    PartNumber.objects.create(
        part=serial, value="PUMP-001", kind=PartNumber.Kind.OEM, is_primary=True
    )
    loc1 = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка B", code="B-01", storage_allowed=True, is_active=True
    )
    loc_bad = StorageLocation.objects.create(
        name="Зона списания", code="WO", storage_allowed=False, is_active=True
    )
    return {
        "sup": sup, "bulk": bulk, "serial": serial,
        "loc1": loc1, "loc2": loc2, "loc_bad": loc_bad,
    }


def _finalized_line(refs, admin, *, part=None, quantity="10", unit_cost="50", shipping="100"):
    part = part or refs["bulk"]
    batch = Batch.objects.create(supplier=refs["sup"], shipping_cost=Decimal(shipping))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


# --- Приёмка (receive) -------------------------------------------------------


def test_receive_part_item_creates_movement_and_available(refs, admin):
    line = _finalized_line(refs, admin, part=refs["serial"], quantity="2")
    item = create_part_items(line, 1)[0]
    assert item.status == PartItem.Status.RECEIVING

    receive_part_item(item, to_location=refs["loc1"], by=admin)
    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert item.current_location == refs["loc1"]

    mv = StockMovement.objects.get(part_item=item)
    assert mv.movement_type == StockMovement.MovementType.RECEIVE_ITEM
    assert mv.quantity == Decimal("1.000")
    assert mv.from_location is None
    assert mv.to_location == refs["loc1"]


def test_receive_stock_lot_creates_movement(refs, admin):
    # qty 10, unit 50, shipping 100 => landed_unit 60; total = 60 * 5 = 300.
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))

    receive_stock_lot(lot, by=admin)
    lot.refresh_from_db()
    assert lot.status == StockLot.Status.AVAILABLE

    mv = StockMovement.objects.get(stock_lot=lot)
    assert mv.movement_type == StockMovement.MovementType.RECEIVE_LOT
    assert mv.quantity == Decimal("5.000")
    assert mv.unit_cost_rub == Decimal("60.00")
    assert mv.total_cost_rub == Decimal("300.00")  # = unit_cost × quantity


def test_receive_twice_rejected(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    receive_stock_lot(lot, by=admin)
    with pytest.raises(InventoryError):
        receive_stock_lot(lot, by=admin)


def test_receive_into_bad_location_rejected(refs, admin):
    line = _finalized_line(refs, admin, part=refs["serial"], quantity="1")
    item = create_part_items(line, 1)[0]
    with pytest.raises(InventoryError):
        receive_part_item(item, to_location=refs["loc_bad"], by=admin)


# --- Append-only журнал ------------------------------------------------------


def test_movement_is_append_only(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    receive_stock_lot(lot, by=admin)
    mv = StockMovement.objects.get(stock_lot=lot)

    mv.comment = "правка"
    with pytest.raises(RuntimeError):
        mv.save()
    with pytest.raises(RuntimeError):
        mv.delete()


def test_movement_item_xor_lot_constraint(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StockMovement.objects.create(
                movement_type=StockMovement.MovementType.ADJUST_IN,
                part_type=lot.part_type, batch=lot.batch, batch_line=lot.batch_line,
                quantity=Decimal("1"),  # ни part_item, ни stock_lot — нарушает XOR
            )


# --- Кэш остатков: обновление, пересборка, сверка ----------------------------


def test_balance_updated_after_receive(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    receive_stock_lot(lot, by=admin)

    bal = StockBalance.objects.get(batch_line=line, location=refs["loc1"])
    assert bal.quantity_physical == Decimal("5.000")
    assert bal.quantity_available == Decimal("5.000")
    assert bal.quantity_quarantine == Decimal("0.000")


def test_rebuild_balance_from_primary_including_legacy(refs, admin):
    # Лот заведён без движений (легаси Слоёв 8–9) — баланс всё равно собирается.
    line = _finalized_line(refs, admin)
    create_stock_lot(line, refs["loc1"], Decimal("5"))
    assert not StockBalance.objects.exists()

    rebuild_stock_balance()
    bal = StockBalance.objects.get(batch_line=line, location=refs["loc1"])
    assert bal.quantity_physical == Decimal("5.000")

    # Идемпотентность: повторный прогон не плодит строк.
    rebuild_stock_balance()
    assert StockBalance.objects.filter(batch_line=line).count() == 1


def test_quarantine_reduces_available(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    receive_stock_lot(lot, by=admin)
    lot.status = StockLot.Status.QUARANTINE
    lot.save(update_fields=["status", "updated_at"])

    rebuild_stock_balance()
    bal = StockBalance.objects.get(batch_line=line, location=refs["loc1"])
    assert bal.quantity_physical == Decimal("5.000")
    assert bal.quantity_quarantine == Decimal("5.000")
    assert bal.quantity_available == Decimal("0.000")


def test_check_finds_discrepancy(refs, admin):
    line = _finalized_line(refs, admin)
    create_stock_lot(line, refs["loc1"], Decimal("5"))
    rebuild_stock_balance()
    assert check_stock_balance() == []

    bal = StockBalance.objects.get(batch_line=line, location=refs["loc1"])
    bal.quantity_physical = Decimal("99")
    bal.save(update_fields=["quantity_physical"])
    assert check_stock_balance()  # непустой список расхождений


# --- Перемещение -------------------------------------------------------------


def test_move_part_item(refs, admin):
    line = _finalized_line(refs, admin, part=refs["serial"], quantity="2")
    item = create_part_items(line, 1)[0]
    receive_part_item(item, to_location=refs["loc1"], by=admin)

    move_part_item(item, refs["loc2"], by=admin)
    item.refresh_from_db()
    assert item.current_location == refs["loc2"]

    mv = StockMovement.objects.get(part_item=item, movement_type="move_item")
    assert mv.from_location == refs["loc1"]
    assert mv.to_location == refs["loc2"]

    assert not StockBalance.objects.filter(batch_line=line, location=refs["loc1"]).exists()
    bal = StockBalance.objects.get(batch_line=line, location=refs["loc2"])
    assert bal.quantity_physical == Decimal("1.000")


def test_move_stock_lot_and_clash(refs, admin):
    line = _finalized_line(refs, admin, quantity="10")
    lot = create_stock_lot(line, refs["loc1"], Decimal("4"))
    receive_stock_lot(lot, by=admin)

    move_stock_lot(lot, refs["loc2"], by=admin)
    lot.refresh_from_db()
    assert lot.location == refs["loc2"]
    assert not StockBalance.objects.filter(batch_line=line, location=refs["loc1"]).exists()
    assert (
        StockBalance.objects.get(batch_line=line, location=refs["loc2"]).quantity_physical
        == Decimal("4.000")
    )

    # Второй лот той же строки в loc1; перенос в loc2 (где уже лот этой строки) — отказ.
    lot2 = create_stock_lot(line, refs["loc1"], Decimal("3"))
    with pytest.raises(InventoryError):
        move_stock_lot(lot2, refs["loc2"], by=admin)


def test_move_to_bad_location_rejected(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("4"))
    receive_stock_lot(lot, by=admin)
    with pytest.raises(InventoryError):
        move_stock_lot(lot, refs["loc_bad"], by=admin)


# --- Корректировка -----------------------------------------------------------


def test_cannot_go_negative_and_depleted(refs, admin):
    line = _finalized_line(refs, admin, quantity="10")
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    receive_stock_lot(lot, by=admin)

    with pytest.raises(InventoryError):
        adjust_stock_lot_quantity(lot, Decimal("-6"), by=admin, comment="ошибка")
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5.000")  # остаток цел

    adjust_stock_lot_quantity(lot, Decimal("-5"), by=admin, comment="расход")
    lot.refresh_from_db()
    assert lot.quantity == Decimal("0.000")
    assert lot.status == StockLot.Status.DEPLETED
    # Обнулённый лот не считается в физическом остатке — строка кэша удалена.
    assert not StockBalance.objects.filter(batch_line=line, location=refs["loc1"]).exists()


def test_adjust_requires_comment(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    receive_stock_lot(lot, by=admin)
    with pytest.raises(InventoryError):
        adjust_stock_lot_quantity(lot, Decimal("-1"), by=admin, comment="")


# --- Границы: всё через сервисы ----------------------------------------------


def test_direct_change_does_not_journal(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    # Прямое изменение количества мимо сервиса не пишет движение.
    lot.quantity = Decimal("3")
    lot.save(update_fields=["quantity", "updated_at"])
    assert StockMovement.objects.filter(stock_lot=lot).count() == 0


def test_backfill_opening_movements_idempotent(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("5"))
    serial_line = _finalized_line(refs, admin, part=refs["serial"], quantity="1")
    item = create_part_items(serial_line, 1, current_location=refs["loc1"])[0]

    created = backfill_opening_movements()
    assert created == 2
    assert StockMovement.objects.filter(stock_lot=lot).count() == 1
    assert StockMovement.objects.filter(part_item=item).count() == 1
    # Повторный прогон ничего не дублирует.
    assert backfill_opening_movements() == 0


# --- Админка: append-only / read-only ----------------------------------------


def test_admin_movement_and_balance_locked():
    mv_admin = StockMovementAdmin(StockMovement, dj_admin.site)
    assert mv_admin.has_add_permission(None) is False
    assert mv_admin.has_change_permission(None) is False
    assert mv_admin.has_delete_permission(None) is False

    bal_admin = StockBalanceAdmin(StockBalance, dj_admin.site)
    assert bal_admin.has_add_permission(None) is False
    assert bal_admin.has_change_permission(None) is False
    assert bal_admin.has_delete_permission(None) is False


# --- Экраны и права ----------------------------------------------------------


def test_cost_hidden_in_movements_from_storekeeper(make_user, client, refs, admin):
    line = _finalized_line(refs, admin)  # landed_unit 60
    lot = create_stock_lot(line, refs["loc1"], Decimal("10"))
    receive_stock_lot(lot, by=admin)  # total = 60 * 10 = 600

    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("movement_list")).content.decode()
    assert "600" not in html

    client.logout()
    client.login(username="admin", password=PASSWORD)
    admin_html = client.get(reverse("movement_list")).content.decode()
    assert "600" in admin_html


def test_movement_list_shows_exact_article_and_whole_values(client, refs, admin):
    line = _finalized_line(
        refs, admin, quantity="1", unit_cost="2645", shipping="0"
    )
    lot = create_stock_lot(line, refs["loc1"], Decimal("1"))
    receive_stock_lot(lot, by=admin)
    client.login(username="admin", password=PASSWORD)
    html = client.get(reverse("movement_list")).content.decode()

    assert '<span class="code-pill">BOLT-001</span>' in html
    assert f"лот #{lot.pk}" not in html
    assert '<td class="num--qty">1</td>' in html
    assert '<td class="num--money">2645</td>' in html
    assert "1,000" not in html and "2645,00" not in html


def test_movement_brp_replacement_does_not_replace_exact_number(client, refs, admin):
    catalog = BrpCatalogPart.objects.create(
        material_no="420931285",
        part_desc="OIL SEAL",
        retail_price_usd=Decimal("10"),
        replacement_no_1="420931284",
    )
    part = promote_brp(catalog, by=admin)
    line = _finalized_line(refs, admin, part=part, quantity="1", shipping="0")
    lot = create_stock_lot(line, refs["loc1"], Decimal("1"))
    receive_stock_lot(lot, by=admin)
    client.login(username="admin", password=PASSWORD)
    html = client.get(reverse("movement_list")).content.decode()
    assert "420931285" in html
    assert "420931284" not in html


def test_movement_polaris_superseded_does_not_replace_exact_number(client, refs, admin):
    catalog = PolarisCatalogPart.objects.create(
        part_number="POL-EXACT",
        part_name="POLARIS SEAL",
        superseded_number="POL-OLD",
        retail_price_usd=Decimal("10"),
    )
    part = promote_polaris(catalog, by=admin)
    line = _finalized_line(refs, admin, part=part, quantity="1", shipping="0")
    lot = create_stock_lot(line, refs["loc1"], Decimal("1"))
    receive_stock_lot(lot, by=admin)
    client.login(username="admin", password=PASSWORD)
    html = client.get(reverse("movement_list")).content.decode()
    assert "POL-EXACT" in html
    assert "POL-OLD" not in html


def test_storekeeper_can_operate_viewer_cannot(make_user, client, refs, admin):
    line = _finalized_line(refs, admin, part=refs["serial"], quantity="1")
    item = create_part_items(line, 1)[0]

    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(
        reverse("item_receive", args=[item.pk]), {"to_location": refs["loc1"].pk}
    )
    assert resp.status_code == 302
    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE

    client.logout()
    make_user("nabl", role=roles.VIEWER)
    client.login(username="nabl", password=PASSWORD)
    # Наблюдатель только смотрит: проводящие действия запрещены.
    assert client.get(reverse("movement_list")).status_code == 200
    assert client.get(reverse("balance_list")).status_code == 200
    resp = client.post(reverse("item_move", args=[item.pk]), {"to_location": refs["loc2"].pk})
    assert resp.status_code == 403


def test_seller_cannot_see_section(make_user, client):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("movement_list")).status_code == 403
    assert client.get(reverse("balance_list")).status_code == 403

    dash = client.get(reverse("dashboard")).content.decode()
    assert "Движения" not in dash
    assert "Остатки" not in dash


def test_nav_section_visibility(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    dash = client.get(reverse("dashboard")).content.decode()
    assert "Движения" in dash
    assert "Остатки" in dash
