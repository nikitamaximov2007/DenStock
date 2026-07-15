"""Слой 14 — перемещение деталей и лотов через сканер.

Покрывает обязательные проверки плана 14-layer-14-inventory-movement.md §11.
Главное: действие после скана идёт ТОЛЬКО через сервисы Слоя 10
(`move_part_item`/`move_stock_lot`); view не пишет StockMovement напрямую
(тест-мок). Hidden/query-поля недоверенные — сервер всё перепроверяет.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.core.models import UnresolvedScan
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import (
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
URL = reverse("scanner_move")
MOVE_ITEM = StockMovement.MovementType.MOVE_ITEM
MOVE_LOT = StockMovement.MovementType.MOVE_LOT


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
    cat = Category.objects.create(name="Узлы")
    unit = Unit.objects.get(name="Штука")
    serial = PartType.objects.create(
        name="Насос", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    serial2 = PartType.objects.create(
        name="Стартер", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    bulk = PartType.objects.create(
        name="Болт", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
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
    loc_inactive = StorageLocation.objects.create(
        name="Архив", code="ARCH", storage_allowed=True, is_active=False
    )
    return {
        "sup": sup, "serial": serial, "serial2": serial2, "bulk": bulk,
        "loc1": loc1, "loc2": loc2, "loc_bad": loc_bad, "loc_inactive": loc_inactive,
    }


def _finalized_line(sup, part, admin, *, qty="5", unit_cost="100", shipping="40"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal(shipping))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(qty), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


def _available_item(refs, admin, loc, *, line=None):
    """Принятый (available) экземпляр в ячейке `loc`."""
    line = line or _finalized_line(refs["sup"], refs["serial"], admin)
    item = create_part_items(line, 1)[0]
    receive_part_item(item, to_location=loc, by=admin)
    item.refresh_from_db()
    return item


def _receiving_item(refs, admin):
    """Непринятый (receiving) экземпляр без ячейки."""
    line = _finalized_line(refs["sup"], refs["serial"], admin)
    return create_part_items(line, 1)[0]


def _available_lot(refs, admin, loc, *, qty="5"):
    """Принятый (available) лот в ячейке `loc`."""
    line = _finalized_line(refs["sup"], refs["bulk"], admin, qty="10")
    lot = create_stock_lot(line, loc, Decimal(qty))
    receive_stock_lot(lot, by=admin)
    lot.refresh_from_db()
    return lot


def _login(client, make_user, role=roles.STOREKEEPER, username="sklad"):
    make_user(username, role=role)
    client.login(username=username, password=PASSWORD)


# --- Доступ ------------------------------------------------------------------


def test_move_page_requires_login(client):
    resp = client.get(URL)
    assert resp.status_code == 302
    assert "/login/" in resp.url


def test_storekeeper_can_open(client, make_user):
    _login(client, make_user)
    assert client.get(URL).status_code == 200


def test_seller_cannot_open(client, make_user):
    _login(client, make_user, role=roles.SELLER, username="prodavec")
    assert client.get(URL).status_code == 403


def test_nav_hidden_for_seller(client, make_user):
    make_user("sklad", role=roles.STOREKEEPER)
    make_user("prodavec", role=roles.SELLER)
    client.login(username="sklad", password=PASSWORD)
    assert "Перемещение" in client.get(reverse("dashboard")).content.decode()
    client.logout()
    client.login(username="prodavec", password=PASSWORD)
    assert "Перемещение" not in client.get(reverse("dashboard")).content.decode()


# --- PartItem: перемещение сканером ------------------------------------------


def test_move_item_scan_location_confirm(client, make_user, refs, admin):
    item = _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)

    # Шаг 1 — скан экземпляра.
    r1 = client.post(URL, {"action": "scan", "code": item.internal_number})
    assert r1.status_code == 200
    assert r1.context["object"].pk == item.pk
    assert r1.context["step"] == "scan_location"

    # Шаг 2 — скан новой ячейки.
    r2 = client.post(URL, {
        "action": "scan", "code": refs["loc2"].code,
        "object_kind": "part_item", "object_id": item.pk,
    })
    assert r2.status_code == 200
    assert r2.context["step"] == "confirm"
    assert r2.context["location"].pk == refs["loc2"].pk

    # Шаг 3 — подтверждение.
    r3 = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": refs["loc2"].pk,
    })
    assert r3.status_code == 302

    item.refresh_from_db()
    assert item.current_location_id == refs["loc2"].pk
    assert StockMovement.objects.filter(part_item=item, movement_type=MOVE_ITEM).count() == 1
    mv = StockMovement.objects.get(part_item=item, movement_type=MOVE_ITEM)
    assert mv.from_location_id == refs["loc1"].pk
    assert mv.to_location_id == refs["loc2"].pk
    assert mv.quantity == Decimal("1.000")
    assert mv.created_by_id is not None


def test_move_item_updates_balance(client, make_user, refs, admin):
    item = _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": refs["loc2"].pk,
    })
    bl = item.batch_line
    assert StockBalance.objects.filter(batch_line=bl, location=refs["loc2"]).exists()
    assert not StockBalance.objects.filter(batch_line=bl, location=refs["loc1"]).exists()


def test_receiving_item_not_moved_via_move_screen(client, make_user, refs, admin):
    item = _receiving_item(refs, admin)
    _login(client, make_user)
    # Скан receiving-экземпляра → объект не выбирается, сообщение про приёмку.
    resp = client.post(URL, {"action": "scan", "code": item.internal_number})
    assert resp.status_code == 200
    assert resp.context["object"] is None
    assert "Приёмку" in resp.context["error"]
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0


def test_same_location_is_noop(client, make_user, refs, admin):
    item = _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": refs["loc1"].pk,
    })
    assert resp.status_code == 200  # не redirect: no-op, не успех
    assert resp.context["info"]
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0


def test_unknown_scan_no_movement(client, make_user, refs, admin):
    _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    resp = client.post(URL, {"action": "scan", "code": "ZZZ-НЕТ-ТАКОГО"})
    assert resp.status_code == 200
    assert resp.context["object"] is None
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0
    assert UnresolvedScan.objects.filter(raw_value="ZZZ-НЕТ-ТАКОГО").exists()


def test_ambiguous_scan_no_movement(client, make_user, refs, admin):
    line1 = _finalized_line(refs["sup"], refs["serial"], admin, qty="1")
    line2 = _finalized_line(refs["sup"], refs["serial2"], admin, qty="1")
    create_part_items(line1, 1, serial_number="SN-DUP")
    create_part_items(line2, 1, serial_number="SN-DUP")
    _login(client, make_user)
    resp = client.post(URL, {"action": "scan", "code": "SN-DUP"})
    assert resp.status_code == 200
    assert resp.context["object"] is None
    assert resp.context["candidates"]
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0


def test_cannot_move_to_storage_forbidden(client, make_user, refs, admin):
    item = _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": refs["loc_bad"].pk,
    })
    assert resp.status_code == 200
    assert resp.context["error"]
    item.refresh_from_db()
    assert item.current_location_id == refs["loc1"].pk
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0


def test_cannot_move_to_inactive_location(client, make_user, refs, admin):
    item = _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": refs["loc_inactive"].pk,
    })
    assert resp.status_code == 200
    assert resp.context["error"]
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0


def test_cannot_move_terminal_item(client, make_user, refs, admin):
    item = _available_item(refs, admin, refs["loc1"])
    PartItem.objects.filter(pk=item.pk).update(status=PartItem.Status.SOLD)
    _login(client, make_user)
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": refs["loc2"].pk,
    })
    assert resp.status_code == 200
    assert resp.context["error"]
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0


def test_tampered_location_rechecked_by_server(client, make_user, refs, admin):
    # Подмена location_id на запрещённую ячейку — сервер перепроверяет, движения нет.
    item = _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": refs["loc_bad"].pk,
    })
    assert resp.context["error"]
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0


def test_tampered_object_id_rechecked_by_server(client, make_user, refs, admin):
    _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": 999999, "location_id": refs["loc2"].pk,
    })
    assert resp.context["error"]
    assert StockMovement.objects.filter(movement_type=MOVE_ITEM).count() == 0


# --- StockLot: перемещение целиком -------------------------------------------


def test_move_lot_quantity(client, make_user, refs, admin):
    lot = _available_lot(refs, admin, refs["loc1"], qty="5")
    _login(client, make_user)

    r1 = client.post(URL, {"action": "select_lot", "lot_id": lot.pk})
    assert r1.status_code == 200
    assert r1.context["object"].part_type.pk == lot.part_type_id
    assert r1.context["step"] == "scan_location"

    r2 = client.post(URL, {
        "action": "scan", "code": refs["loc2"].code,
        "object_kind": "stock_quantity", "part_id": lot.part_type_id,
        "source_location_id": refs["loc1"].pk, "stock_state": lot.status,
    })
    assert r2.context["step"] == "confirm"

    r3 = client.post(URL, {
        "action": "confirm", "object_kind": "stock_quantity",
        "part_id": lot.part_type_id, "source_location_id": refs["loc1"].pk,
        "stock_state": lot.status, "location_id": refs["loc2"].pk,
        "quantity": "5", "move_token": "lot-quantity-token",
    })
    assert r3.status_code == 302

    lot.refresh_from_db()
    assert lot.location_id == refs["loc1"].pk
    assert lot.quantity == 0
    target_lot = StockLot.objects.get(batch_line=lot.batch_line, location=refs["loc2"])
    assert target_lot.quantity == 5
    mv = StockMovement.objects.get(stock_lot=lot, movement_type=MOVE_LOT)
    assert mv.quantity == Decimal("5.000")  # лот целиком
    assert mv.from_location_id == refs["loc1"].pk
    assert mv.to_location_id == refs["loc2"].pk
    bl = lot.batch_line
    assert StockBalance.objects.filter(batch_line=bl, location=refs["loc2"]).exists()
    assert not StockBalance.objects.filter(batch_line=bl, location=refs["loc1"]).exists()


def test_partial_lot_move_ui(client, make_user, refs, admin):
    lot = _available_lot(refs, admin, refs["loc1"])
    _login(client, make_user)
    selected = client.get(f"{URL}?lot={lot.pk}")
    html = selected.content.decode()
    assert "можно переместить" in html
    confirmed = client.post(
        URL,
        {
            "action": "scan",
            "code": refs["loc2"].code,
            "object_kind": "stock_quantity",
            "part_id": lot.part_type_id,
            "source_location_id": refs["loc1"].pk,
            "stock_state": lot.status,
        },
    )
    assert 'name="quantity"' in confirmed.content.decode()
    assert 'name="delta"' not in html


# --- Контроль архитектуры ----------------------------------------------------


def test_view_uses_service_not_direct_ledger(client, make_user, refs, admin):
    item = _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    before = StockMovement.objects.count()
    transfer = SimpleNamespace(
        quantity=Decimal("1"),
        part_number="Артикул не указан",
        from_location_code=refs["loc1"].code,
        to_location_code=refs["loc2"].code,
    )
    with patch(
        "apps.core.views.perform_stock_transfer", return_value=(transfer, True)
    ) as mock_move:
        resp = client.post(URL, {
            "action": "confirm", "object_kind": "part_item",
            "object_id": item.pk, "location_id": refs["loc2"].pk,
        })
    assert resp.status_code == 302
    assert mock_move.call_count == 1
    # Сервис замокан → view сам движений не создаёт.
    assert StockMovement.objects.count() == before


def test_cost_hidden_without_permission(client, make_user, refs, admin):
    # Кладовщик делает перемещение → в его истории НЕТ сумм.
    item = _available_item(refs, admin, refs["loc1"])
    _login(client, make_user)
    client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": refs["loc2"].pk,
    })
    html = client.get(URL).content.decode()
    # landed_unit = (5×100 + 40) / 5 = 108 → сумма движения (ru-локаль рендерит «108,00»).
    assert "Сумма" not in html
    assert "108" not in html

    # Админ делает своё перемещение → суммы видны.
    item2 = _available_item(refs, admin, refs["loc1"])
    client.logout()
    client.login(username="admin", password=PASSWORD)
    client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item2.pk, "location_id": refs["loc2"].pk,
    })
    admin_html = client.get(URL).content.decode()
    assert "Сумма" in admin_html
    assert "108" in admin_html
