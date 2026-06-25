"""Слой 12 — поступление и размещение через сканер.

Покрывает обязательные проверки плана 12-layer-12-scanner-receiving-placement.md §11.
Главное: действие после скана идёт ТОЛЬКО через сервисы Слоя 10; view не пишет
StockMovement напрямую (тест-мок). Hidden-поля недоверенные — сервер всё
перепроверяет на подтверждении.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.core.models import UnresolvedScan
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import create_part_items, create_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
URL = "/scanner/receiving/"


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


def _finalized_line(sup, part, admin, *, qty="2", unit_cost="100", shipping="40"):
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


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
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
    loc_ok = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    loc_ok2 = StorageLocation.objects.create(
        name="Ячейка B", code="B-01", storage_allowed=True, is_active=True
    )
    loc_bad = StorageLocation.objects.create(
        name="Зона списания", code="WO", storage_allowed=False, is_active=True
    )
    loc_inactive = StorageLocation.objects.create(
        name="Старая", code="OLD", storage_allowed=True, is_active=False
    )

    iline = _finalized_line(sup, serial, admin, qty="2")
    item = create_part_items(iline, 1, serial_number="SN-1")[0]
    create_part_items(iline, 1, serial_number="DUP")  # серийник-дубль (вид serial)
    iline2 = _finalized_line(sup, serial2, admin, qty="1")
    create_part_items(iline2, 1, serial_number="DUP")  # тот же серийник у другого вида

    bline = _finalized_line(sup, bulk, admin, qty="10")
    lot = create_stock_lot(bline, loc_ok, Decimal("5"))

    return {
        "item": item, "lot": lot,
        "loc_ok": loc_ok, "loc_ok2": loc_ok2, "loc_bad": loc_bad, "loc_inactive": loc_inactive,
    }


def _login(client, make_user, role=roles.STOREKEEPER, username="sklad"):
    make_user(username, role=role)
    client.login(username=username, password=PASSWORD)


# --- Доступ ------------------------------------------------------------------


def test_receiving_requires_login(client):
    assert client.get(URL).status_code == 302


def test_storekeeper_can_open(client, make_user):
    _login(client, make_user)
    assert client.get(URL).status_code == 200


def test_seller_cannot_open(client, make_user):
    _login(client, make_user, role=roles.SELLER, username="prodavec")
    assert client.get(URL).status_code == 403
    assert "Приёмка сканером" not in client.get(reverse("dashboard")).content.decode()


def test_viewer_cannot_open(client, make_user):
    _login(client, make_user, role=roles.VIEWER, username="nabl")
    assert client.get(URL).status_code == 403


# --- Приёмка PartItem --------------------------------------------------------


def test_receive_part_item_full_flow(client, make_user, data):
    _login(client, make_user)
    item = data["item"]
    # шаг 1: скан экземпляра
    r1 = client.post(URL, {"action": "scan", "code": item.internal_number})
    assert r1.status_code == 200
    html1 = r1.content.decode()
    assert 'name="object_id"' in html1 and f'value="{item.pk}"' in html1
    # шаг 2: скан ячейки
    r2 = client.post(URL, {
        "action": "scan", "code": data["loc_ok"].code,
        "object_kind": "part_item", "object_id": item.pk,
    })
    assert r2.status_code == 200
    # шаг 3: подтверждение
    r3 = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": data["loc_ok"].pk,
    })
    assert r3.status_code == 302  # успех → redirect
    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert item.current_location_id == data["loc_ok"].pk
    assert StockMovement.objects.filter(
        movement_type="receive_item", part_item=item
    ).count() == 1


def test_receive_part_item_updates_balance(client, make_user, data):
    _login(client, make_user)
    item = data["item"]
    client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": data["loc_ok"].pk,
    })
    assert StockBalance.objects.filter(
        batch_line=item.batch_line, location=data["loc_ok"]
    ).exists()


def test_repeat_receive_no_duplicate_movement(client, make_user, data):
    _login(client, make_user)
    item = data["item"]
    confirm = {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": data["loc_ok"].pk,
    }
    client.post(URL, confirm)  # первая приёмка
    assert StockMovement.objects.filter(movement_type="receive_item", part_item=item).count() == 1

    before = StockMovement.objects.count()
    resp = client.post(URL, confirm)  # повтор: уже available, та же ячейка → move отклонён
    assert resp.status_code == 200  # ошибка, не redirect
    assert StockMovement.objects.filter(movement_type="receive_item", part_item=item).count() == 1
    assert StockMovement.objects.count() == before  # дубля движения нет


# --- Приёмка StockLot --------------------------------------------------------


def test_select_lot_then_receive(client, make_user, data):
    _login(client, make_user)
    lot = data["lot"]
    # выбрать лот из UI
    r1 = client.post(URL, {"action": "select_lot", "lot_id": lot.pk})
    assert r1.status_code == 200
    assert "Отсканируйте ячейку" in r1.content.decode()
    # подтвердить в его ячейке (loc_ok)
    r2 = client.post(URL, {
        "action": "confirm", "object_kind": "stock_lot",
        "object_id": lot.pk, "location_id": data["loc_ok"].pk,
    })
    assert r2.status_code == 302
    lot.refresh_from_db()
    assert lot.status == StockLot.Status.AVAILABLE
    assert StockMovement.objects.filter(movement_type="receive_lot", stock_lot=lot).count() == 1
    assert StockBalance.objects.filter(
        batch_line=lot.batch_line, location=data["loc_ok"]
    ).exists()


# --- Главный контроль: view не пишет ledger напрямую -------------------------


def test_view_delegates_to_service_no_direct_movement(client, make_user, data):
    _login(client, make_user)
    item = data["item"]
    before = StockMovement.objects.count()
    with patch("apps.core.views.receive_part_item") as mock_recv:
        resp = client.post(URL, {
            "action": "confirm", "object_kind": "part_item",
            "object_id": item.pk, "location_id": data["loc_ok"].pk,
        })
    # View вызвала сервис ровно один раз...
    mock_recv.assert_called_once()
    # ...и при замоканном сервисе сама не создала ни одного движения.
    assert StockMovement.objects.count() == before
    assert resp.status_code == 302


# --- Ошибки скана не приводят к действию -------------------------------------


def test_unknown_scan_no_movement(client, make_user, data):
    _login(client, make_user)
    before = StockMovement.objects.count()
    before_u = UnresolvedScan.objects.count()
    resp = client.post(URL, {"action": "scan", "code": "МУСОР-XYZ"})
    assert resp.status_code == 200
    assert "не распознан" in resp.content.decode().lower()
    assert StockMovement.objects.count() == before
    assert UnresolvedScan.objects.count() == before_u + 1  # журналируется


def test_ambiguous_scan_no_movement(client, make_user, data):
    _login(client, make_user)
    before = StockMovement.objects.count()
    resp = client.post(URL, {"action": "scan", "code": "DUP"})  # серийник у двух видов
    assert resp.status_code == 200
    assert "select_candidate" in resp.content.decode()  # показаны варианты
    assert StockMovement.objects.count() == before


# --- Защита от недоверенных hidden-полей -------------------------------------


def _assert_no_action(resp, before):
    assert resp.status_code == 200  # ошибка → перерисовка, не redirect
    assert StockMovement.objects.count() == before


def test_confirm_nonexistent_item_id(client, make_user, data):
    _login(client, make_user)
    before = StockMovement.objects.count()
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": 999999, "location_id": data["loc_ok"].pk,
    })
    _assert_no_action(resp, before)
    assert "Не выбран объект" in resp.content.decode()


def test_confirm_nonexistent_location_id(client, make_user, data):
    _login(client, make_user)
    before = StockMovement.objects.count()
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": data["item"].pk, "location_id": 999999,
    })
    _assert_no_action(resp, before)
    assert "Не отсканирована ячейка" in resp.content.decode()


def test_confirm_storage_not_allowed_location(client, make_user, data):
    _login(client, make_user)
    before = StockMovement.objects.count()
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": data["item"].pk, "location_id": data["loc_bad"].pk,
    })
    _assert_no_action(resp, before)


def test_confirm_inactive_location(client, make_user, data):
    _login(client, make_user)
    before = StockMovement.objects.count()
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": data["item"].pk, "location_id": data["loc_inactive"].pk,
    })
    _assert_no_action(resp, before)


def test_confirm_lot_wrong_cell(client, make_user, data):
    _login(client, make_user)
    lot = data["lot"]  # создан в loc_ok
    before = StockMovement.objects.count()
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "stock_lot",
        "object_id": lot.pk, "location_id": data["loc_ok2"].pk,  # другая ячейка
    })
    _assert_no_action(resp, before)
    assert "создан для ячейки" in resp.content.decode()
    lot.refresh_from_db()
    assert lot.status == StockLot.Status.RECEIVING  # не принят


def test_confirm_without_object(client, make_user, data):
    _login(client, make_user)
    before = StockMovement.objects.count()
    resp = client.post(URL, {"action": "confirm", "location_id": data["loc_ok"].pk})
    _assert_no_action(resp, before)
    assert "Не выбран объект" in resp.content.decode()


def test_confirm_without_location(client, make_user, data):
    _login(client, make_user)
    before = StockMovement.objects.count()
    resp = client.post(URL, {
        "action": "confirm", "object_kind": "part_item", "object_id": data["item"].pk,
    })
    _assert_no_action(resp, before)
    assert "Не отсканирована ячейка" in resp.content.decode()


# --- Себестоимость по праву --------------------------------------------------


def test_cost_hidden_for_storekeeper(client, make_user, data):
    _login(client, make_user)
    item = data["item"]  # landed 120
    client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": data["loc_ok"].pk,
    })
    html = client.get(URL).content.decode()
    assert "Сумма" not in html
    assert "120" not in html


def test_cost_visible_for_admin(client, admin, data):
    client.login(username="admin", password=PASSWORD)
    item = data["item"]
    client.post(URL, {
        "action": "confirm", "object_kind": "part_item",
        "object_id": item.pk, "location_id": data["loc_ok"].pk,
    })
    html = client.get(URL).content.decode()
    assert "Сумма" in html
    assert "120" in html
