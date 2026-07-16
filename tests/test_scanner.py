"""Слой 11 — единый резолв сканера (scanner resolve).

Покрывает обязательные проверки плана 11-layer-11-scanner-resolve.md §12.
Главная граница: резолв только распознаёт и возвращает локатор — никаких
складских/коммерческих действий (движений/остатков/продаж) не выполняет.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartBarcode, PartNumber, PartType, Unit
from apps.core.models import UnresolvedScan
from apps.core.scanner import resolve_scan
from apps.inventory.models import StockBalance, StockMovement
from apps.inventory.services import create_part_items
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
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    serial = PartType.objects.create(
        name="Насос", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    PartBarcode.objects.create(part=serial, value="4607123456789")
    PartNumber.objects.create(part=serial, value="0 986-221.047", kind=PartNumber.Kind.OEM)

    # Неоднозначный OEM: один и тот же нормализованный номер у двух видов.
    amb_a = PartType.objects.create(
        name="Аналог A", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    amb_b = PartType.objects.create(
        name="Аналог B", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    PartNumber.objects.create(part=amb_a, value="ABC-100", kind=PartNumber.Kind.OEM)
    PartNumber.objects.create(part=amb_b, value="abc 100", kind=PartNumber.Kind.ANALOG)

    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )

    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("40"))
    line = BatchLine.objects.create(
        batch=batch, part_type=serial,
        quantity=Decimal("2"), unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    item = create_part_items(line, 1, serial_number="SN-77")[0]

    return {
        "serial": serial, "amb_a": amb_a, "amb_b": amb_b,
        "loc": loc, "batch": batch, "item": item,
    }


# --- Чистый резолвер (resolve_scan) ------------------------------------------


def test_resolve_item_internal_barcode(data):
    item = data["item"]
    r = resolve_scan(item.internal_barcode)  # ITEM:DS-000001
    assert r.status == "found" and r.type == "part_item" and r.id == item.pk


def test_resolve_item_internal_number(data):
    item = data["item"]
    r = resolve_scan(item.internal_number)  # DS-000001
    assert r.type == "part_item" and r.id == item.pk


def test_resolve_location_barcode(data):
    loc = data["loc"]
    r = resolve_scan(loc.barcode)  # LOC:A-01
    assert r.type == "location" and r.id == loc.pk


def test_resolve_location_code(data):
    loc = data["loc"]
    r = resolve_scan(loc.code)  # A-01
    assert r.type == "location" and r.id == loc.pk


def test_resolve_batch_number(data):
    batch = data["batch"]
    r = resolve_scan(batch.number)  # П-000001
    assert r.type == "batch" and r.id == batch.pk


def test_resolve_factory_barcode(data):
    r = resolve_scan("4607123456789")
    assert r.type == "part_type" and r.id == data["serial"].pk


def test_resolve_oem_with_normalization(data):
    # Хранится «0 986-221.047», нормализуется в «0986221047».
    r = resolve_scan("0986 221 047")
    assert r.type == "part_type" and r.id == data["serial"].pk


def test_resolve_serial_number(data):
    item = data["item"]
    r = resolve_scan("SN-77")
    assert r.type == "part_item" and r.id == item.pk


def test_resolve_ambiguous_oem(data):
    r = resolve_scan("ABC100")
    assert r.status == "ambiguous"
    assert r.found is False
    assert len(r.candidates) == 2
    assert {c["type"] for c in r.candidates} == {"part_type"}


def test_resolve_unknown_is_pure(data):
    before = UnresolvedScan.objects.count()
    r = resolve_scan("НЕТ-ТАКОГО-КОДА-999")
    assert r.status == "unknown" and r.found is False
    # Чистый сервис журнал не трогает.
    assert UnresolvedScan.objects.count() == before


# --- Endpoint ----------------------------------------------------------------


def test_endpoint_requires_login(client):
    resp = client.post(reverse("scanner_resolve"), {"code": "DS-000001"})
    assert resp.status_code == 302  # редирект на вход


def test_endpoint_empty_input_400(make_user, client):
    make_user("u")
    client.login(username="u", password=PASSWORD)
    resp = client.post(reverse("scanner_resolve"), {"code": "   "})
    assert resp.status_code == 400
    assert resp.json()["status"] == "error"


def test_endpoint_found_json(make_user, client, data):
    make_user("u")
    client.login(username="u", password=PASSWORD)
    resp = client.post(reverse("scanner_resolve"), {"code": data["item"].internal_number})
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["type"] == "part_item"
    assert body["url"]


def test_endpoint_payload_has_no_cost(make_user, client, data):
    # Кладовщик без can_view_purchase_cost: payload структурно без денежных полей.
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("scanner_resolve"), {"code": data["item"].internal_number})
    body = resp.json()
    assert set(body.keys()) == {
        "found", "status", "type", "id", "label", "url", "message", "candidates",
    }


def test_endpoint_unknown_creates_unresolved(make_user, client):
    make_user("u")
    client.login(username="u", password=PASSWORD)
    before = UnresolvedScan.objects.count()
    resp = client.post(
        reverse("scanner_resolve"), {"code": "МУСОР-123", "context": "topbar"}
    )
    body = resp.json()
    assert body["found"] is False and body["status"] == "unknown"
    assert UnresolvedScan.objects.count() == before + 1
    scan = UnresolvedScan.objects.latest("created_at")
    assert scan.raw_value == "МУСОР-123"
    assert scan.normalized_value == "МУСОР123"  # save() нормализует
    assert scan.context == "topbar"


def test_endpoint_unknown_antispam(make_user, client):
    make_user("u")
    client.login(username="u", password=PASSWORD)
    before = UnresolvedScan.objects.count()
    client.post(reverse("scanner_resolve"), {"code": "ДУБЛЬ-1"})
    client.post(reverse("scanner_resolve"), {"code": "ДУБЛЬ-1"})  # в пределах ~5 c
    assert UnresolvedScan.objects.count() == before + 1  # дубль не записан


# --- Страница и доступ -------------------------------------------------------


def test_scanner_page_login_and_render(make_user, client):
    assert client.get(reverse("scanner")).status_code == 302
    make_user("u")
    client.login(username="u", password=PASSWORD)
    response = client.get(reverse("scanner"))
    assert response.status_code == 302
    assert response.url == reverse("part_search")


def test_scanner_page_post_redirects_code_to_unified_search(make_user, client, data):
    make_user("u")
    client.login(username="u", password=PASSWORD)
    resp = client.post(reverse("scanner"), {"code": data["batch"].number})
    assert resp.status_code == 302
    assert resp.url == f"{reverse('part_search')}?q=%D0%9F-000001"


def test_unresolved_list_admin_manager_only(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    assert client.get(reverse("unresolved_list")).status_code == 403

    client.logout()
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    assert client.get(reverse("unresolved_list")).status_code == 200


def test_nav_scanner_visible_unresolved_gated(make_user, client):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    dash = client.get(reverse("dashboard")).content.decode()
    assert 'href="/search/"' in dash
    assert 'href="/scanner/"' not in dash
    assert "Нераспознанные" not in dash

    client.logout()
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    dash2 = client.get(reverse("dashboard")).content.decode()
    assert ">Настройки<" in dash2
    tools = client.get(reverse("unresolved_list")).content.decode()
    assert "Инструменты / Нераспознанные" in tools


# --- Граница: резолв НЕ выполняет складских действий -------------------------


def test_resolve_does_not_touch_ledger(make_user, client, data):
    make_user("u")
    client.login(username="u", password=PASSWORD)
    before_m = StockMovement.objects.count()
    before_b = StockBalance.objects.count()
    for code in [
        data["item"].internal_number, data["loc"].code, data["batch"].number,
        "4607123456789", "SN-77", "МУСОР-XYZ",
    ]:
        client.post(reverse("scanner_resolve"), {"code": code})
    # Ни одного нового движения/остатка: сканер только распознаёт.
    assert StockMovement.objects.count() == before_m
    assert StockBalance.objects.count() == before_b
