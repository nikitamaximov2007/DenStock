"""Приёмка сканером: довнесение найденной детали +1 к существующей ячейке.

Гарантии: обычный номер детали (BRP material_no) не даёт ошибку «это вид
детали», а предлагает положить +1 туда, где деталь уже лежит; подтверждение
добавляет ровно +1 (движение ADJUST_IN с тегом found_addition); двойной сабмит
не добавляет +2; несколько ячеек требуют выбора; если детали нет ни в одной
ячейке — молча не добавляем; в «Поступлениях» ничего не появляется; exact
material_no не подменяется заменой.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.inventory.models import StockMovement
from apps.inventory.services import add_found_stock, create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.receipts.models import Receipt
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


def _stock(part, location, qty, sup, admin):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(str(qty)),
        unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="Стартовый ввод")
    loc3 = StorageLocation.objects.create(
        name="C03", code="S04-L03-D01-C03", storage_allowed=True, is_active=True
    )
    loc4 = StorageLocation.objects.create(
        name="C04", code="S04-L03-D01-C04", storage_allowed=True, is_active=True
    )
    # Деталь с заменой: 420931285 (OEM) / 420931284 (ANALOG) — проверяем identity.
    brp = BrpCatalogPart.objects.create(
        material_no="420931285", part_desc="OIL SEAL",
        retail_price_usd=Decimal("10"), replacement_no_1="420931284",
    )
    part = promote_to_warehouse(brp, by=admin)  # recommended_price 10*105*1.4 = 1470
    lot4 = _stock(part, loc4, 3, sup, admin)  # деталь лежит в C04
    return {"sup": sup, "loc3": loc3, "loc4": loc4, "part": part, "lot4": lot4, "admin": admin}


def _login(client, make_user, *, role=None, superuser=False, name="u"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


# --- Сервис -------------------------------------------------------------------------


def test_add_found_stock_adds_one(data):
    before = StockMovement.objects.count()
    lot, movement = add_found_stock(data["part"], data["loc4"], by=data["admin"])
    data["lot4"].refresh_from_db()
    assert data["lot4"].quantity == Decimal("4")  # было 3, стало 4
    assert lot.pk == data["lot4"].pk
    assert movement.movement_type == StockMovement.MovementType.ADJUST_IN
    assert movement.document_type == "found_addition"
    assert movement.to_location == data["loc4"]
    assert movement.created_by == data["admin"]
    assert StockMovement.objects.count() == before + 1


def test_add_found_stock_requires_existing_lot(data):
    from apps.inventory.services import InventoryError

    with pytest.raises(InventoryError, match="ещё нет в выбранной ячейке"):
        add_found_stock(data["part"], data["loc3"], by=data["admin"])  # в C03 детали нет
    assert not StockMovement.objects.filter(document_type="found_addition").exists()


# --- Экран приёмки ------------------------------------------------------------------


def test_scan_part_number_offers_addition_not_error(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(URL, {"action": "scan", "code": "420931285"})
    html = resp.content.decode()
    assert "Это вид детали, а не конкретный экземпляр" not in html  # старой ошибки нет
    assert "Добавить +1 к наличию" in html
    assert "S04-L03-D01-C04" in html  # ячейка, где деталь уже лежит
    assert "420931285" in html
    assert "420931284" not in html  # замена не подменяет номер
    # Токен идемпотентности выставлен в сессии.
    assert client.session.get("found_add_token")


def test_confirm_adds_exactly_one(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    token = client.session["found_add_token"]
    resp = client.post(URL, {
        "action": "add_found", "part_id": data["part"].pk,
        "location_id": data["loc4"].pk, "scanned_number": "420931285", "token": token,
    }, follow=True)
    assert "Добавлено +1: 420931285 OIL SEAL в ячейку S04-L03-D01-C04" in resp.content.decode()
    data["lot4"].refresh_from_db()
    assert data["lot4"].quantity == Decimal("4")


def test_double_submit_does_not_add_two(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    token = client.session["found_add_token"]
    payload = {
        "action": "add_found", "part_id": data["part"].pk,
        "location_id": data["loc4"].pk, "scanned_number": "420931285", "token": token,
    }
    client.post(URL, payload)  # первый сабмит: +1
    resp = client.post(URL, payload)  # повтор той же формы: должен быть отклонён
    assert "не добавлена ещё раз" in resp.content.decode()
    data["lot4"].refresh_from_db()
    assert data["lot4"].quantity == Decimal("4")  # ровно +1, не +2


def test_multiple_cells_require_choice(client, make_user, data):
    # Та же деталь ещё и в C03 — теперь две ячейки.
    _stock(data["part"], data["loc3"], 2, data["sup"], data["admin"])
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(URL, {"action": "scan", "code": "420931285"})
    html = resp.content.decode()
    assert "Деталь найдена в нескольких ячейках" in html
    assert "S04-L03-D01-C03" in html and "S04-L03-D01-C04" in html
    # Радио не предвыбрано (нужен явный выбор).
    assert "checked" not in html.split("Ячейка")[1].split("</table>")[0]


def test_part_not_in_any_cell_not_added_silently(client, make_user, admin, data):
    orphan_brp = BrpCatalogPart.objects.create(
        material_no="999000111", part_desc="LONELY", retail_price_usd=Decimal("5"),
    )
    orphan = promote_to_warehouse(orphan_brp, by=admin)  # карточка есть, остатка нет
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(URL, {"action": "scan", "code": "999000111"})
    html = resp.content.decode()
    assert "Этой детали ещё нет ни в одной ячейке" in html
    assert "Добавить +1" not in html
    assert not StockMovement.objects.filter(part_type=orphan).exists()


def test_addition_does_not_create_receipt(client, make_user, data):
    receipts_before = Receipt.objects.count()
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    token = client.session["found_add_token"]
    client.post(URL, {
        "action": "add_found", "part_id": data["part"].pk,
        "location_id": data["loc4"].pk, "scanned_number": "420931285", "token": token,
    })
    assert Receipt.objects.count() == receipts_before  # «Поступлений» не прибавилось


def test_serial_item_flow_unaffected(client, make_user, data):
    # Сканирование ITEM:/DS-… (несуществующий) не уходит в довнесение.
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(URL, {"action": "scan", "code": "ITEM:DS-NOPE"})
    html = resp.content.decode()
    assert "Добавить +1 к наличию" not in html  # это не сценарий довнесения


def test_actions_overview_shows_new_balance(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    token = client.session["found_add_token"]
    client.post(URL, {
        "action": "add_found", "part_id": data["part"].pk,
        "location_id": data["loc4"].pk, "scanned_number": "420931285", "token": token,
    })
    html = client.get(reverse("actions_scan") + "?q=420931285").content.decode()
    assert "S04-L03-D01-C04" in html
    assert "Доступно всего: 4" in html  # обновлённый остаток


def test_found_history_shown_on_receiving(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    token = client.session["found_add_token"]
    client.post(URL, {
        "action": "add_found", "part_id": data["part"].pk,
        "location_id": data["loc4"].pk, "scanned_number": "420931285", "token": token,
    })
    html = client.get(URL).content.decode()
    assert "Добавление найденных деталей" in html
    assert "420931285" in html
    assert "S04-L03-D01-C04" in html


def test_receiving_requires_inventory_permission(client, make_user, data):
    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.get(URL).status_code == 403
