"""Batch scanner receiving of exact warehouse, BRP, and Polaris identities."""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts import roles
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import PartType
from apps.core.receiving_queue import PENDING_SESSION_KEY, QUEUE_SESSION_KEY
from apps.inventory.models import FoundStockPosting, StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    add_found_stock,
    create_stock_lot,
    post_found_stock_group,
    receive_stock_lot,
)
from apps.polaris.models import PolarisCatalogPart, PolarisPartLink
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


# --- Helpers -----------------------------------------------------------------------


def _queue(client):
    return client.session[QUEUE_SESSION_KEY]


def _single_line(client):
    return next(iter(_queue(client)["lines"].values()))


def _group_payload(client, location):
    client.get(URL)  # rendering creates/refreshes the group token
    token = _queue(client)["group_tokens"][str(location.pk)]["token"]
    return {"action": "queue_post", "location_id": location.pk, "token": token}


# --- Queue behavior ----------------------------------------------------------------


def test_scan_exact_part_queues_without_stock_mutation(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    movements = StockMovement.objects.count()
    receipts = Receipt.objects.count()
    response = client.post(URL, {"action": "scan", "code": "420931285"}, follow=True)

    line = _single_line(client)
    assert response.status_code == 200
    assert line["exact_number"] == "420931285"
    assert line["location_id"] == data["loc4"].pk
    assert line["quantity"] == 1
    assert "420931284" not in response.content.decode()
    assert StockMovement.objects.count() == movements
    assert Receipt.objects.count() == receipts


def test_repeat_scan_groups_quantity_and_survives_refresh(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    client.post(URL, {"action": "scan", "code": "420931285"})
    assert _single_line(client)["quantity"] == 2
    html = client.get(URL).content.decode()
    assert "К добавлению · 2 шт." in html
    assert _single_line(client)["quantity"] == 2


def test_queue_page_query_count_is_bounded(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    with CaptureQueriesContext(connection) as captured:
        response = client.get(URL)
    assert response.status_code == 200
    assert len(captured) == 9


def test_queue_isolated_between_users(client, django_user_model, make_user, data):
    _login(client, make_user, superuser=True, name="first")
    client.post(URL, {"action": "scan", "code": "420931285"})
    first_session = client.session.session_key

    client.logout()
    second = django_user_model.objects.create_superuser("second", password=PASSWORD)
    assert second
    client.login(username="second", password=PASSWORD)
    assert QUEUE_SESSION_KEY not in client.session
    assert client.session.session_key != first_session


def test_remove_and_clear_queue_do_not_change_stock(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    before = StockMovement.objects.count()
    client.post(URL, {"action": "scan", "code": "420931285"})
    line = _single_line(client)
    client.post(URL, {"action": "queue_remove", "line_id": line["id"]})
    assert not _queue(client)["lines"]
    client.post(URL, {"action": "scan", "code": "420931285"})
    client.post(URL, {"action": "queue_clear"})
    assert not _queue(client)["lines"]
    assert StockMovement.objects.count() == before


def test_quantity_must_be_positive_integer(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    line = _single_line(client)
    response = client.post(
        URL,
        {"action": "queue_update", "line_id": line["id"], "quantity": "0"},
    )
    assert "положительным целым" in response.content.decode()
    assert _single_line(client)["quantity"] == 1


# --- Locations and independent groups ----------------------------------------------


def test_multiple_cells_require_explicit_choice(client, make_user, data):
    _stock(data["part"], data["loc3"], 2, data["sup"], data["admin"])
    _login(client, make_user, superuser=True, name="boss")
    response = client.post(
        URL, {"action": "scan", "code": "420931285"}, follow=True
    )
    line = _single_line(client)
    html = response.content.decode()
    assert line["location_id"] is None
    assert {choice["id"] for choice in line["location_choices"]} == {
        data["loc3"].pk,
        data["loc4"].pk,
    }
    assert "Деталь найдена в нескольких ячейках" in html
    assert "checked" not in html


def test_assign_choice_places_line_in_correct_group(client, make_user, data):
    _stock(data["part"], data["loc3"], 2, data["sup"], data["admin"])
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    line = _single_line(client)
    client.post(
        URL,
        {"action": "queue_assign", "line_id": line["id"], "location_id": data["loc3"].pk},
    )
    assert _single_line(client)["location_id"] == data["loc3"].pk
    assert "Ячейка <span class=\"code-pill\">S04-L03-D01-C03" in client.get(URL).content.decode()


def test_cannot_assign_inactive_or_unrelated_location(client, make_user, data):
    inactive = StorageLocation.objects.create(
        name="Архив", code="S04-L03-D01-C05", storage_allowed=True, is_active=False
    )
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    line = _single_line(client)
    response = client.post(
        URL,
        {"action": "queue_assign", "line_id": line["id"], "location_id": inactive.pk},
    )
    assert "активную ячейку" in response.content.decode()
    assert _single_line(client)["location_id"] == data["loc4"].pk


def test_posting_one_location_leaves_other_group(client, make_user, data):
    brp2 = BrpCatalogPart.objects.create(
        material_no="420999999", part_desc="FILTER", retail_price_usd=Decimal("8")
    )
    part2 = promote_to_warehouse(brp2, by=data["admin"])
    lot3 = _stock(part2, data["loc3"], 1, data["sup"], data["admin"])
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    client.post(URL, {"action": "scan", "code": "420999999"})
    html = client.get(URL).content.decode()
    assert html.index("S04-L03-D01-C03") < html.index("S04-L03-D01-C04")

    client.post(URL, _group_payload(client, data["loc4"]))
    data["lot4"].refresh_from_db()
    lot3.refresh_from_db()
    assert data["lot4"].quantity == Decimal("4")
    assert lot3.quantity == Decimal("1")
    assert {line["location_id"] for line in _queue(client)["lines"].values()} == {
        data["loc3"].pk
    }


# --- New catalog identities ---------------------------------------------------------


def test_new_brp_part_can_create_first_lot_without_receipt(client, make_user, data):
    brp = BrpCatalogPart.objects.create(
        material_no="999000111", part_desc="LONELY", retail_price_usd=Decimal("5")
    )
    _login(client, make_user, superuser=True, name="boss")
    receipts_before = Receipt.objects.count()
    client.post(URL, {"action": "scan", "code": brp.material_no})
    line = _single_line(client)
    assert line["location_id"] is None and line["location_mode"] == "new"
    client.post(
        URL,
        {"action": "queue_update", "line_id": line["id"], "quantity": 3},
    )
    client.post(
        URL,
        {
            "action": "queue_assign",
            "line_id": line["id"],
            "location_code": data["loc3"].code,
        },
    )
    response = client.post(URL, _group_payload(client, data["loc3"]), follow=True)

    part = brp.links.get().part
    lot = StockLot.objects.get(part_type=part, location=data["loc3"])
    movement = StockMovement.objects.get(stock_lot=lot, document_type="found_addition")
    assert lot.quantity == Decimal("3")
    assert movement.movement_type == StockMovement.MovementType.ADJUST_IN
    assert movement.quantity == Decimal("3")
    assert Receipt.objects.count() == receipts_before
    assert "Новые остатки: 999000111: 3" in response.content.decode()


def test_new_polaris_part_can_create_first_lot(client, make_user, data):
    polaris = PolarisCatalogPart.objects.create(
        part_number="POL-700", part_name="POLARIS SEAL", retail_price_usd=Decimal("7")
    )
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": polaris.part_number})
    line = _single_line(client)
    client.post(
        URL,
        {
            "action": "queue_assign",
            "line_id": line["id"],
            "location_code": data["loc3"].code,
        },
    )
    client.post(URL, _group_payload(client, data["loc3"]))
    link = PolarisPartLink.objects.get(polaris_part=polaris)
    assert StockLot.objects.get(part_type=link.part, location=data["loc3"]).quantity == 1


def test_new_part_location_can_change_before_posting(client, make_user, data):
    brp = BrpCatalogPart.objects.create(
        material_no="CHANGE-CELL", part_desc="CHANGE CELL", retail_price_usd=Decimal("3")
    )
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": brp.material_no})
    line = _single_line(client)
    client.post(
        URL,
        {
            "action": "queue_assign",
            "line_id": line["id"],
            "location_code": data["loc3"].code,
        },
    )
    client.post(URL, {"action": "queue_unassign", "line_id": line["id"]})
    assert _single_line(client)["location_id"] is None
    client.post(
        URL,
        {
            "action": "queue_assign",
            "line_id": line["id"],
            "location_code": data["loc4"].code,
        },
    )
    assert _single_line(client)["location_id"] == data["loc4"].pk
    assert not StockMovement.objects.filter(document_type="found_addition").exists()


def test_unknown_code_does_not_create_anonymous_part(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    part_count = PartType.objects.count()
    movement_count = StockMovement.objects.count()
    response = client.post(URL, {"action": "scan", "code": "NO-SUCH-777"})
    assert "не найден в каталогах BRP, Polaris" in response.content.decode()
    assert PartType.objects.count() == part_count
    assert StockMovement.objects.count() == movement_count


def test_same_number_in_brp_and_polaris_requires_source_choice(client, make_user, data):
    BrpCatalogPart.objects.create(
        material_no="SHARED-1", part_desc="BRP PART", retail_price_usd=Decimal("5")
    )
    PolarisCatalogPart.objects.create(
        part_number="SHARED-1", part_name="POLARIS PART", retail_price_usd=Decimal("6")
    )
    _login(client, make_user, superuser=True, name="boss")
    response = client.post(URL, {"action": "scan", "code": "SHARED-1"}, follow=True)
    pending = client.session[PENDING_SESSION_KEY]
    assert not client.session.get(QUEUE_SESSION_KEY, {"lines": {}})["lines"]
    brp_key = next(key for key in pending["candidates"] if key.startswith("brp:"))
    assert any(key.startswith("polaris:") for key in pending["candidates"])
    assert "Выберите производителя" in response.content.decode()
    client.post(
        URL,
        {
            "action": "queue_select_candidate",
            "candidate_token": pending["token"],
            "candidate_key": brp_key,
        },
    )
    assert _single_line(client)["source"] == "brp"
    assert _single_line(client)["manufacturer"] == "BRP"


# --- Atomicity and idempotency ------------------------------------------------------


def test_group_post_and_double_submit_are_idempotent(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    payload = _group_payload(client, data["loc4"])
    first = client.post(URL, payload, follow=True)
    second = client.post(URL, payload, follow=True)
    data["lot4"].refresh_from_db()
    assert "Добавлено 1 деталей" in first.content.decode()
    assert "Эта группа уже была проведена" in second.content.decode()
    assert data["lot4"].quantity == Decimal("4")
    assert FoundStockPosting.objects.filter(token=payload["token"]).count() == 1


def test_group_location_hidden_field_cannot_be_substituted(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    payload = _group_payload(client, data["loc4"])
    payload["location_id"] = data["loc3"].pk
    response = client.post(URL, payload)
    data["lot4"].refresh_from_db()
    assert "Группа изменилась" in response.content.decode()
    assert data["lot4"].quantity == Decimal("3")


def test_service_rolls_back_entire_group_on_second_adjustment(monkeypatch, data):
    from apps.inventory import services as inventory_services

    brp2 = BrpCatalogPart.objects.create(
        material_no="ATOMIC-2", part_desc="SECOND", retail_price_usd=Decimal("4")
    )
    part2 = promote_to_warehouse(brp2, by=data["admin"])
    lot2 = _stock(part2, data["loc4"], 2, data["sup"], data["admin"])
    before_first = data["lot4"].quantity
    before_second = lot2.quantity
    movements_before = StockMovement.objects.count()
    entries = [
        {
            "source": "brp",
            "source_id": data["part"].brp_link.brp_part_id,
            "exact_number": "420931285",
            "quantity": 2,
        },
        {
            "source": "brp",
            "source_id": brp2.pk,
            "exact_number": "ATOMIC-2",
            "quantity": 1,
        },
    ]

    original_adjust = inventory_services.adjust_stock_lot_quantity
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InventoryError("Ошибка второй строки")
        return original_adjust(*args, **kwargs)

    monkeypatch.setattr(inventory_services, "adjust_stock_lot_quantity", fail_second)
    with pytest.raises(InventoryError):
        post_found_stock_group(
            entries=entries,
            location=data["loc4"],
            token="atomic-group-token",
            by=data["admin"],
        )
    data["lot4"].refresh_from_db()
    lot2.refresh_from_db()
    assert data["lot4"].quantity == before_first
    assert lot2.quantity == before_second
    assert StockMovement.objects.count() == movements_before
    assert not FoundStockPosting.objects.filter(token="atomic-group-token").exists()


def test_service_rejects_nonpositive_quantity(data):
    entry = {
        "source": "brp",
        "source_id": data["part"].brp_link.brp_part_id,
        "exact_number": "420931285",
        "quantity": -1,
    }
    with pytest.raises(InventoryError, match="положительным целым"):
        post_found_stock_group(
            entries=[entry],
            location=data["loc4"],
            token="bad-quantity-token",
            by=data["admin"],
        )


# --- Regressions --------------------------------------------------------------------


def test_serial_item_flow_unaffected(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    response = client.post(URL, {"action": "scan", "code": "ITEM:DS-NOPE"})
    assert "К добавлению" not in response.content.decode()
    assert not client.session.get(QUEUE_SESSION_KEY, {"lines": {}})["lines"]


def test_actions_overview_and_history_show_new_balance(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    client.post(URL, {"action": "scan", "code": "420931285"})
    client.post(URL, _group_payload(client, data["loc4"]))
    actions_html = client.get(reverse("actions_scan") + "?q=420931285").content.decode()
    receiving_html = client.get(URL).content.decode()
    assert "Доступно всего: 4" in actions_html
    assert "Добавление найденных деталей" in receiving_html
    assert "420931285" in receiving_html
    assert "S04-L03-D01-C04" in receiving_html
    assert "+1" in receiving_html


def test_receiving_requires_inventory_permission(client, make_user, data):
    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.get(URL).status_code == 403
