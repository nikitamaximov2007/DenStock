from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartBarcode, PartNumber, PartType, Unit
from apps.catalog.search import search_part_ids
from apps.core.part_picker import looks_like_name_query, search_pickable_parts
from apps.counting.models import InventoryCountingSession
from apps.inventory.models import StockLot, StockMovement
from apps.warehouse.addresses import get_or_create_location

PASSWORD = "picker-test-password"


@pytest.fixture
def operator(db, django_user_model):
    user = django_user_model.objects.create_user("picker", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    return user


@pytest.fixture
def parts(db):
    category = Category.objects.create(name="Уплотнения")
    unit = Unit.objects.get(name="Штука")

    def make(name, *, active=True, number="", barcode=""):
        part = PartType.objects.create(
            name=name,
            category=category,
            unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            is_active=active,
            recommended_price=Decimal("125.00"),
        )
        if number:
            PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM)
        if barcode:
            PartBarcode.objects.create(part=part, value=barcode)
        return part

    return {
        "seal": make("Сальник 25*62*7", number="25*62*7", barcode="P0-QA-25627"),
        "seal_wide": make("Сальник 25*62*10"),
        "seal_other": make("Сальник 62*7 усиленный"),
        "archived": make("Сальник 25*62*7 старый", active=False),
        "english": make("Seal 25*62*7"),
        "xss": make("<script>alert(1)</script> Сальник"),
    }


@pytest.fixture
def count_session(db):
    location = get_or_create_location("S01-D03-C08", name="Тестовая ячейка")
    return InventoryCountingSession.objects.create(
        storage_location=location,
        full_address=location.code,
        title="Проверка поиска",
    )


def test_picker_search_matches_cyrillic_latin_case_and_compact_names(parts):
    ids = {row["id"] for row in search_pickable_parts("САЛЬНИК")}
    assert {parts["seal"].pk, parts["seal_wide"].pk, parts["seal_other"].pk} <= ids
    assert parts["archived"].pk not in ids

    assert parts["seal"].pk in {row["id"] for row in search_pickable_parts("62*7")}
    assert parts["english"].pk in {row["id"] for row in search_pickable_parts("seal")}


def test_picker_filters_archived_cards_before_the_bounded_search_window(db):
    category = Category.objects.create(name="Archived search window")
    unit = Unit.objects.get(name="Штука")
    for index in range(65):
        PartType.objects.create(
            name=f"Seal archived {index:03}",
            category=category,
            unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            is_active=False,
        )
    active = PartType.objects.create(
        name="Seal currently stocked",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    assert active.pk in {row["id"] for row in search_pickable_parts("seal")}


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL trigram search")
def test_active_only_fuzzy_sql_searches_only_active_cards(db):
    category = Category.objects.create(name="Active fuzzy search")
    unit = Unit.objects.get(name="Штука")
    archived = PartType.objects.create(
        name="BEARING DRIVE 90001",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_active=False,
    )
    active = PartType.objects.create(
        name="BEARING DRIVE 90002",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    ids = {hit.part_id for hit in search_part_ids("bearng", active_only=True)}
    assert active.pk in ids
    assert archived.pk not in ids


def test_name_classifier_keeps_code_like_unknowns_on_scanner_path():
    assert looks_like_name_query("сальник")
    assert looks_like_name_query("seal")
    assert looks_like_name_query("62*7")
    assert not looks_like_name_query("MУСОР-XYZ")
    assert not looks_like_name_query("1234567890")


def test_part_search_endpoint_requires_inventory_permission(
    client, operator, parts, django_user_model
):
    client.force_login(operator)
    response = client.get(reverse("scanner_part_search"), {"q": "seal"})
    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == parts["english"].pk
    assert response["Content-Type"].startswith("application/json")

    seller = django_user_model.objects.create_user("seller", password=PASSWORD)
    seller.groups.add(Group.objects.get(name=roles.SELLER))
    client.force_login(seller)
    assert client.get(reverse("scanner_part_search"), {"q": "seal"}).status_code == 403


def test_receiving_pick_uses_concrete_card_and_deduplicates_token(client, operator, parts):
    client.force_login(operator)
    before = StockMovement.objects.count()

    first = client.post(
        reverse("scanner_receiving"),
        {"action": "pick_part", "part_id": parts["seal"].pk, "pick_token": "once-1"},
    )
    assert first.status_code == 302
    queue = client.session["batch_receiving_queue_v1"]
    assert len(queue["lines"]) == 1
    row = next(iter(queue["lines"].values()))
    assert row["part_id"] == parts["seal"].pk
    assert row["name"] == parts["seal"].name

    client.post(
        reverse("scanner_receiving"),
        {"action": "pick_part", "part_id": parts["seal"].pk, "pick_token": "once-1"},
    )
    assert next(iter(client.session["batch_receiving_queue_v1"]["lines"].values()))[
        "quantity"
    ] == 1
    assert StockMovement.objects.count() == before

    client.post(
        reverse("scanner_receiving"),
        {"action": "pick_part", "part_id": parts["seal"].pk, "pick_token": "once-2"},
    )
    line_id = next(iter(client.session["batch_receiving_queue_v1"]["lines"]))
    assert client.session["batch_receiving_queue_v1"]["lines"][line_id]["quantity"] == 2
    assert StockMovement.objects.count() == before

    location = get_or_create_location("S01-D03-C08", name="Приёмка")
    client.post(
        reverse("scanner_receiving"),
        {"action": "queue_assign", "line_id": line_id, "location_id": location.pk},
    )
    receiving_page = client.get(reverse("scanner_receiving"))
    group = receiving_page.context["receiving_queue"]["groups"][0]
    response = client.post(
        reverse("scanner_receiving"),
        {"action": "queue_post", "location_id": location.pk, "token": group["token"]},
    )
    assert response.status_code == 302, [str(message) for message in response.context["messages"]]
    received = StockLot.objects.get(part_type=parts["seal"], location=location)
    assert received.quantity == Decimal("2")
    assert StockMovement.objects.count() == before + 1


def test_known_barcode_still_uses_fast_receiving_scan_path(client, operator, parts):
    client.force_login(operator)
    response = client.post(
        reverse("scanner_receiving"),
        {"action": "scan", "code": "P0-QA-25627"},
    )
    assert response.status_code == 302
    queue = client.session["batch_receiving_queue_v1"]
    line = next(iter(queue["lines"].values()))
    assert line["part_id"] == parts["seal"].pk
    assert line["exact_number"] == "25*62*7"
    assert line["quantity"] == 1


def test_receiving_pick_requires_token_and_csrf(client, operator, parts):
    client.force_login(operator)
    response = client.post(
        reverse("scanner_receiving"),
        {"action": "pick_part", "part_id": parts["seal"].pk},
    )
    assert response.status_code == 302
    assert "batch_receiving_queue_v1" not in client.session

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(operator)
    response = csrf_client.post(
        reverse("scanner_receiving"),
        {"action": "pick_part", "part_id": parts["seal"].pk, "pick_token": "csrf-test"},
    )
    assert response.status_code == 403


def test_counting_pick_records_selected_part_and_accumulates_once_per_request(
    client, operator, parts, count_session
):
    client.force_login(operator)
    before = StockMovement.objects.count()
    url = reverse("counting_pick", args=[count_session.pk])

    client.post(url, {"part_id": parts["seal"].pk, "request_token": "count-once-1"})
    client.post(url, {"part_id": parts["seal"].pk, "request_token": "count-once-1"})
    line = count_session.lines.get()
    assert line.warehouse_part_id == parts["seal"].pk
    assert line.quantity_counted == Decimal("1")
    assert line.source == "warehouse"
    assert line.display_name == parts["seal"].name
    assert not count_session.lines.filter(source="unknown").exists()

    client.post(url, {"part_id": parts["seal"].pk, "request_token": "count-once-2"})
    line.refresh_from_db()
    assert line.quantity_counted == Decimal("2")
    assert StockMovement.objects.count() == before


def test_exact_special_identifier_keeps_existing_scan_behavior(
    client, operator, parts, count_session
):
    client.force_login(operator)
    response = client.post(
        reverse("counting_scan", args=[count_session.pk]),
        {"code": "25*62*7", "request_token": "exact-star-code"},
    )
    assert response.status_code == 302
    line = count_session.lines.get()
    assert line.warehouse_part_id == parts["seal"].pk
    assert line.scanned_value == "25*62*7"


def test_name_search_permission_and_archived_part_cannot_be_picked(client, parts, operator):
    client.force_login(operator)
    response = client.get(reverse("scanner_part_search"), {"q": "сальник"})
    ids = {row["id"] for row in response.json()["results"]}
    assert parts["archived"].pk not in ids

    response = client.post(
        reverse("scanner_receiving"),
        {"action": "pick_part", "part_id": parts["archived"].pk, "pick_token": "archived"},
        follow=True,
    )
    assert "не найдена или в архиве" in response.content.decode().lower()
    assert "batch_receiving_queue_v1" not in client.session


def test_name_suggestion_renders_xss_name_as_text(client, operator, parts):
    client.force_login(operator)
    response = client.post(reverse("scanner_receiving"), {"action": "scan", "code": "сальник"})
    assert response.status_code == 200
    html = response.content.decode()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
