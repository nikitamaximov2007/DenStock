"""Слой 13 — быстрый поиск детали (read-only).

Покрывает обязательные проверки плана 13-layer-13-fast-part-search.md §12.
Ключевое: поиск ничего не пишет (нет движений, баланс не меняется) и НЕ удваивает
остаток (кэш StockBalance ИЛИ первичка, не их сумма).
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import (
    Category,
    PartBarcode,
    PartCompatibility,
    PartNumber,
    PartType,
    Unit,
    VehicleMake,
    VehicleModel,
    VehicleType,
)
from apps.core.search import search_parts
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
    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка B", code="B-01", storage_allowed=True, is_active=True
    )

    serial = PartType.objects.create(
        name="Насос-Поиск", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("500"), min_price=Decimal("400"),
    )
    PartBarcode.objects.create(part=serial, value="4607123456789")
    PartNumber.objects.create(part=serial, value="0 986-221.047", kind=PartNumber.Kind.OEM)
    vt = VehicleType.objects.create(name="Экскаватор")
    mk = VehicleMake.objects.create(vehicle_type=vt, name="Komatsu")
    md = VehicleModel.objects.create(vehicle_make=mk, name="PC200")
    PartCompatibility.objects.create(part=serial, vehicle_model=md)

    iline = _finalized_line(sup, serial, admin, qty="2")  # landed_unit 120
    item = create_part_items(iline, 1, serial_number="SN-1")[0]
    receive_part_item(item, to_location=loc, by=admin)  # → StockBalance physical 1

    bulk_cache = PartType.objects.create(
        name="Болт-Кэш", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    bline = _finalized_line(sup, bulk_cache, admin, qty="10")
    lot_cache = create_stock_lot(bline, loc, Decimal("5"))
    receive_stock_lot(lot_cache, by=admin)  # → StockBalance physical 5

    bulk_fallback = PartType.objects.create(
        name="Болт-Перв", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    bline2 = _finalized_line(sup, bulk_fallback, admin, qty="10")
    create_stock_lot(bline2, loc2, Decimal("7"))  # receiving, БЕЗ строк баланса

    return {
        "serial": serial, "item": item, "serial_batch": iline.batch,
        "bulk_cache": bulk_cache, "bulk_fallback": bulk_fallback,
        "loc": loc, "loc2": loc2,
    }


def _row(rows, part):
    return next(r for r in rows if r.part.pk == part.pk)


# --- Сопоставление запроса ---------------------------------------------------


def test_search_by_name(data):
    rows = search_parts("Насос-Поиск")
    assert any(r.part.pk == data["serial"].pk for r in rows)


def test_search_oem_normalized(data):
    rows = search_parts("0986 221 047")  # нормализуется в 0986221047
    assert any(r.part.pk == data["serial"].pk for r in rows)


def test_search_barcode(data):
    rows = search_parts("4607123456789")
    assert any(r.part.pk == data["serial"].pk for r in rows)


def test_search_internal_number(data):
    rows = search_parts(data["item"].internal_number)
    assert any(r.part.pk == data["serial"].pk for r in rows)


def test_search_serial_number(data):
    rows = search_parts("SN-1")
    assert any(r.part.pk == data["serial"].pk for r in rows)


def test_search_vehicle_model(data):
    rows = search_parts("PC200")
    assert any(r.part.pk == data["serial"].pk for r in rows)


def test_unknown_query_empty(data):
    assert search_parts("несуществующий-zzz-000") == []


def test_short_query_empty(data):
    assert search_parts("Н") == []


# --- Наличие -----------------------------------------------------------------


def test_shows_physical_location_batch(data):
    row = _row(search_parts("Насос-Поиск"), data["serial"])
    assert row.physical == Decimal("1")
    assert data["loc"].code in row.locations
    assert data["serial_batch"].number in row.batches


def test_no_double_count_with_balance(data):
    # Лот принят → есть строка StockBalance (physical 5) И первичный StockLot (5).
    # Поиск должен показать 5, а не 5+5=10.
    row = _row(search_parts("Болт-Кэш"), data["bulk_cache"])
    assert row.source == "balance"
    assert row.physical == Decimal("5")
    assert StockBalance.objects.filter(part_type=data["bulk_cache"]).exists()


def test_fallback_without_balance(data):
    # Лот создан, но не принят → строк StockBalance нет → считаем из первички.
    row = _row(search_parts("Болт-Перв"), data["bulk_fallback"])
    assert row.source == "primary"
    assert row.physical == Decimal("7")
    assert row.receiving == Decimal("7")
    assert not StockBalance.objects.filter(part_type=data["bulk_fallback"]).exists()


# --- Права и себестоимость ---------------------------------------------------


def test_search_requires_login(client):
    assert client.get(reverse("part_search")).status_code == 302


def test_seller_can_search_without_inventory_links(make_user, client, data):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    resp = client.get(reverse("part_search"), {"q": "Насос-Поиск"})
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Насос-Поиск" in html  # сводка видна
    assert data["loc"].code in html  # ячейки видны
    # ссылок на инвентарные карточки (item_detail) нет — иначе 403
    assert reverse("item_detail", args=[data["item"].pk]) not in html


def test_cost_hidden_for_storekeeper(make_user, client, data):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("part_search"), {"q": "Насос-Поиск"}).content.decode()
    assert "Себестоимость" not in html
    assert "120" not in html


def test_cost_visible_for_manager(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("part_search"), {"q": "Насос-Поиск"}).content.decode()
    assert "Себестоимость" in html
    assert "120" in html


def test_empty_query_message(make_user, client, data):
    make_user("u")
    client.login(username="u", password=PASSWORD)
    html = client.get(reverse("part_search"), {"q": "zzz-нет-такого"}).content.decode()
    assert "Ничего не найдено" in html


# --- Граница: поиск read-only ------------------------------------------------


def test_search_has_no_side_effects(make_user, client, data):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    m_before = StockMovement.objects.count()
    b_before = sorted(StockBalance.objects.values_list("id", "quantity_physical"))
    data["item"].refresh_from_db()
    item_status = data["item"].status  # фактический статус в БД до поисков
    for q in ["Насос-Поиск", "Болт-Кэш", "Болт-Перв", "0986221047", "SN-1", "zzz"]:
        client.get(reverse("part_search"), {"q": q})
    assert StockMovement.objects.count() == m_before  # движений не создано
    assert sorted(StockBalance.objects.values_list("id", "quantity_physical")) == b_before
    data["item"].refresh_from_db()
    assert data["item"].status == item_status  # статусы не тронуты
