"""Слой 23 — печать складских этикеток.

Ключевой инвариант: слой НЕ меняет сканер (`resolve_scan`), ledger
(`StockMovement`/`StockBalance`) и складскую физику — это только печатное
представление уже существующих кодов. Доступ — под `PRINT_LABELS`; финансов на
этикетках нет; `StockLot` не печатается.
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartBarcode, PartNumber, PartType, Unit
from apps.core.scanner import resolve_scan
from apps.inventory.models import StockBalance, StockMovement
from apps.inventory.services import create_part_items, receive_part_item
from apps.labels.barcode import code128_svg, encode_code128_b, safe_code128_svg
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


def _finalized_line(sup, part, admin, *, qty):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("40"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(qty), unit_cost_currency=Decimal("100"),
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
    serial = PartType.objects.create(
        name="Деталь-А", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL, min_price=Decimal("100"),
    )
    PartNumber.objects.create(
        part=serial, value="OEM-123", kind=PartNumber.Kind.OEM, is_primary=True
    )
    PartBarcode.objects.create(part=serial, value="4600000000017")
    line = _finalized_line(sup, serial, admin, qty="1")
    item = create_part_items(line, 1, serial_number="SN-1")[0]
    receive_part_item(item, to_location=loc, by=admin)
    # Вид детали без заводского ШК — графика штрихкода не должна рисоваться.
    part_nobc = PartType.objects.create(
        name="Деталь-Без-ШК", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    return {"loc": loc, "item": item, "part": serial, "part_nobc": part_nobc}


def _html(resp) -> str:
    return resp.content.decode("utf-8")


# --- Доступ / права ----------------------------------------------------------


def test_print_user_can_open_item_label(make_user, client, data):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.get(reverse("label_item", args=[data["item"].pk]))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")


def test_without_permission_403(make_user, client, data):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("label_item", args=[data["item"].pk])).status_code == 403
    assert client.get(reverse("label_location", args=[data["loc"].pk])).status_code == 403
    assert client.get(reverse("label_part", args=[data["part"].pk])).status_code == 403


def test_viewer_cannot_print(make_user, client, data):
    make_user("nabl", role=roles.VIEWER)
    client.login(username="nabl", password=PASSWORD)
    assert client.get(reverse("label_item", args=[data["item"].pk])).status_code == 403


def test_anonymous_redirected_to_login(client, data):
    assert client.get(reverse("label_item", args=[data["item"].pk])).status_code == 302


def test_nonexistent_object_404(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    assert client.get(reverse("label_item", args=[999999])).status_code == 404


# --- Содержимое этикеток -----------------------------------------------------


def test_item_label_contains_codes(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _html(client.get(reverse("label_item", args=[data["item"].pk])))
    assert data["item"].internal_number in text
    assert data["item"].internal_barcode in text  # ITEM:DS-…
    assert "OEM-123" in text
    assert "SN-1" in text
    assert "A-01" in text  # ячейка в адресе


def test_location_label_contains_code_barcode_address(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _html(client.get(reverse("label_location", args=[data["loc"].pk])))
    assert "A-01" in text  # code
    assert "LOC:A-01" in text  # barcode
    assert data["loc"].full_path in text  # полный адрес


def test_part_label_with_barcode_has_svg_and_value(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _html(client.get(reverse("label_part", args=[data["part"].pk])))
    assert "Деталь-А" in text
    assert "OEM-123" in text
    assert "4600000000017" in text
    assert "<svg" in text  # есть графический штрихкод


def test_part_label_without_barcode_has_no_svg(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _html(client.get(reverse("label_part", args=[data["part_nobc"].pk])))
    assert "Деталь-Без-ШК" in text
    assert "<svg" not in text  # ШК у вида нет → графики нет, только текст


# --- Batch print (простой ?ids=) ---------------------------------------------


def test_items_batch_print(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    pk = data["item"].pk
    # Несуществующий id молча отбрасывается, существующий печатается.
    text = _html(client.get(reverse("label_items") + f"?ids={pk},999999"))
    assert data["item"].internal_barcode in text


def test_items_batch_print_requires_permission(make_user, client, data):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("label_items") + "?ids=1").status_code == 403


# --- StockLot не печатается --------------------------------------------------


def test_no_stocklot_label_route(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Маршрута этикетки лота нет — обращение под /labels/ даёт 404.
    assert client.get("/labels/lot/1/").status_code == 404


# --- Сканер не изменён -------------------------------------------------------


def test_scanner_resolve_unchanged(data):
    # Печатаемый код экземпляра по-прежнему резолвится в тот же объект (слой не трогал сканер).
    res = resolve_scan(data["item"].internal_barcode)
    assert res.found is True
    assert res.type == "part_item"
    assert res.id == data["item"].pk
    loc_res = resolve_scan(data["loc"].barcode)
    assert loc_res.found is True
    assert loc_res.type == "location"
    assert loc_res.id == data["loc"].pk


# --- Read-only ---------------------------------------------------------------


def test_labels_are_read_only(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    item = data["item"]
    item.refresh_from_db()  # актуальный снимок из БД (фикстура принимала экземпляр)
    data["loc"].refresh_from_db()
    mv_before = StockMovement.objects.count()
    bal_before = sorted(StockBalance.objects.values_list("id", "quantity_available"))
    item_before = (item.status, item.current_location_id, item.internal_barcode)
    loc_before = (data["loc"].code, data["loc"].barcode)
    for url in [
        reverse("label_item", args=[item.pk]),
        reverse("label_location", args=[data["loc"].pk]),
        reverse("label_part", args=[data["part"].pk]),
        reverse("label_part", args=[data["part_nobc"].pk]),
        reverse("label_items") + f"?ids={item.pk}",
    ]:
        client.get(url)
    assert StockMovement.objects.count() == mv_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_available")) == bal_before
    item.refresh_from_db()
    assert (item.status, item.current_location_id, item.internal_barcode) == item_before
    data["loc"].refresh_from_db()
    assert (data["loc"].code, data["loc"].barcode) == loc_before


def test_no_stocklot_barcode_field():
    # Правило «StockLot напрямую не сканируем»: у лота нет поля barcode.
    from apps.inventory.models import StockLot

    assert "barcode" not in {f.name for f in StockLot._meta.get_fields()}


def test_no_pending_migrations(db):
    out = StringIO()
    try:
        call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
    except SystemExit:
        pytest.fail(f"Есть несозданные миграции:\n{out.getvalue()}")


# --- Генератор Code128 (детерминизм + контрольная сумма) ---------------------


def test_code128_encode_single_char():
    # 'A' (значение 33): [Start B=104, 33, checksum, Stop=106]; checksum=(104+33*1)%103=34.
    assert encode_code128_b("A") == [104, 33, 34, 106]


def test_code128_encode_checksum_two_chars():
    # 'A','B' → (104 + 33*1 + 34*2) % 103 = 205 % 103 = 102.
    assert encode_code128_b("AB") == [104, 33, 34, 102, 106]


def test_code128_encode_project_code():
    codes = encode_code128_b("ITEM:DS-000001")
    assert codes[0] == 104  # Start B
    assert codes[-1] == 106  # Stop
    assert 0 <= codes[-2] < 103  # контрольная сумма в диапазоне


def test_code128_svg_is_valid():
    svg = code128_svg("LOC:A-01")
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "<rect" in svg  # есть штрихи
    assert 'aria-label="LOC:A-01"' in svg


def test_code128_rejects_non_ascii():
    with pytest.raises(ValueError):
        encode_code128_b("Я")  # вне диапазона Code128 B


def test_safe_svg_degrades_to_empty():
    assert safe_code128_svg("") == ""
    assert safe_code128_svg("Я") == ""  # непригодный код → без графики, без падения
    assert safe_code128_svg("DS-000001").startswith("<svg")
