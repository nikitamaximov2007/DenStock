import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts import roles
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse as promote_brp
from apps.catalog.models import (
    Category,
    Manufacturer,
    PartBarcode,
    PartCompatibility,
    PartNumber,
    PartType,
    Unit,
    VehicleMake,
    VehicleModel,
    VehicleType,
)
from apps.polaris.models import PolarisCatalogPart
from apps.polaris.services import promote_to_warehouse as promote_polaris

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
def refs(db):
    cat = Category.objects.create(name="Двигатель")
    Manufacturer.objects.create(name="Yamaha")
    unit = Unit.objects.get(name="Штука")  # из data-миграции
    vtype = VehicleType.objects.get(name="Снегоход")  # из data-миграции
    make = VehicleMake.objects.create(vehicle_type=vtype, name="Yamaha")
    model = VehicleModel.objects.create(vehicle_make=make, name="VK540")
    return {"cat": cat, "unit": unit, "model": model}


def _payload(refs, **over):
    data = {
        "name": "Топливный насос",
        "category": refs["cat"].pk,
        "unit": refs["unit"].pk,
        "tracking_mode": PartType.TrackingMode.SERIAL,
        "min_stock_level": "0",
    }
    data.update(over)
    return data


def _make_part(refs, **over):
    fields = {
        "name": "Деталь",
        "category": refs["cat"],
        "unit": refs["unit"],
        "tracking_mode": PartType.TrackingMode.SERIAL,
    }
    fields.update(over)
    return PartType.objects.create(**fields)


def test_create_part(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    resp = client.post(reverse("part_create"), _payload(refs))
    assert resp.status_code == 302
    assert PartType.objects.filter(name="Топливный насос").exists()


def test_create_part_with_oem(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    part = _make_part(refs)
    resp = client.post(
        reverse("part_number_add", args=[part.pk]),
        {"value": "ABC-123", "kind": PartNumber.Kind.OEM},
    )
    assert resp.status_code == 302
    num = PartNumber.objects.get(part=part)
    assert num.kind == PartNumber.Kind.OEM
    assert num.normalized_value == "ABC123"


def test_create_part_with_analog(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    part = _make_part(refs)
    client.post(
        reverse("part_number_add", args=[part.pk]),
        {"value": "AN-9", "kind": PartNumber.Kind.ANALOG},
    )
    assert PartNumber.objects.filter(part=part, kind=PartNumber.Kind.ANALOG).exists()


def test_create_part_with_barcode(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    part = _make_part(refs)
    client.post(reverse("part_barcode_add", args=[part.pk]), {"value": "BAR-100"})
    assert PartBarcode.objects.filter(value="BAR-100").exists()


def test_barcode_globally_unique(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    p1 = _make_part(refs, name="A")
    p2 = _make_part(refs, name="B")
    client.post(reverse("part_barcode_add", args=[p1.pk]), {"value": "DUP"})
    client.post(reverse("part_barcode_add", args=[p2.pk]), {"value": "DUP"})
    assert PartBarcode.objects.filter(value="DUP").count() == 1


def test_link_to_vehicle_model(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    part = _make_part(refs)
    client.post(
        reverse("part_compat_add", args=[part.pk]),
        {"vehicle_model": refs["model"].pk, "year_from": "2018", "year_to": "2022"},
    )
    assert PartCompatibility.objects.filter(part=part, vehicle_model=refs["model"]).exists()


def test_tracking_mode_saved(refs):
    part = _make_part(refs, tracking_mode=PartType.TrackingMode.BULK)
    part.refresh_from_db()
    assert part.tracking_mode == PartType.TrackingMode.BULK


def test_prices_saved(refs):
    part = _make_part(refs, recommended_price="100.00", min_price="50.00")
    part.refresh_from_db()
    assert str(part.recommended_price) == "100.00"
    assert str(part.min_price) == "50.00"


def test_min_price_not_greater_than_recommended(refs):
    part = PartType(
        name="X",
        category=refs["cat"],
        unit=refs["unit"],
        recommended_price=50,
        min_price=100,
    )
    with pytest.raises(ValidationError):
        part.full_clean()


def test_no_purchase_cost_on_part():
    names = {f.name for f in PartType._meta.get_fields()}
    assert not any("cost" in n or "purchase" in n for n in names)


def test_deactivation_instead_of_delete(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    part = _make_part(refs)
    resp = client.post(reverse("part_toggle", args=[part.pk]))
    assert resp.status_code == 302
    part.refresh_from_db()
    assert part.is_active is False
    assert PartType.objects.filter(pk=part.pk).exists()


def test_storekeeper_cannot_create(make_user, client, refs):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("part_create"), _payload(refs, name="Запрещено"))
    assert resp.status_code == 403
    assert not PartType.objects.filter(name="Запрещено").exists()


def test_viewer_cannot_edit(make_user, client, refs):
    make_user("nabl", role=roles.VIEWER)
    client.login(username="nabl", password=PASSWORD)
    part = _make_part(refs)
    resp = client.post(reverse("part_edit", args=[part.pk]), _payload(refs))
    assert resp.status_code == 403


def test_storekeeper_can_view(make_user, client, refs):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    part = _make_part(refs)
    assert client.get(reverse("part_list")).status_code == 200
    assert client.get(reverse("part_detail", args=[part.pk])).status_code == 200


def test_search_by_name(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    _make_part(refs, name="Топливный насос Yamaha")
    html = client.get(reverse("part_list"), {"q": "насос"}).content.decode()
    assert "Топливный насос Yamaha" in html


def test_search_by_oem_normalized(make_user, client, refs):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    part = _make_part(refs, name="Деталь с OEM")
    PartNumber.objects.create(part=part, value="ABC-123", kind=PartNumber.Kind.OEM)
    html = client.get(reverse("part_list"), {"q": "abc123"}).content.decode()
    assert "Деталь с OEM" in html


def test_navigation_shows_parts(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("dashboard")).content.decode()
    assert ">Каталог<" not in html
    assert client.get(reverse("part_list")).status_code == 200


# --- Колонка «Артикул» на /parts/ (feature/parts-list-article-column) -----------------


KIND = PartNumber.Kind


def _admin(make_user, client):
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)


def _list_html(client, **params):
    return client.get(reverse("part_list"), params).content.decode()


def _row_cells(html, needle):
    """Ячейки <td> строки таблицы, содержащей `needle` (по названию детали)."""
    for row in re.findall(r"<tr>.*?</tr>", html, re.S):
        if needle in row:
            return re.findall(r"<td.*?</td>", row, re.S)
    raise AssertionError(f"строка с {needle!r} не найдена")


def _article_cell(html, needle):
    """Ячейка «Артикул» (вторая колонка) строки детали."""
    return _row_cells(html, needle)[1]


def test_parts_list_has_article_header(make_user, client, refs):
    _admin(make_user, client)
    _make_part(refs, name="Просто деталь")
    html = _list_html(client)
    assert "<th>Артикул</th>" in html


def test_article_header_is_after_name(make_user, client, refs):
    _admin(make_user, client)
    _make_part(refs, name="Просто деталь")
    html = _list_html(client)
    assert html.index("<th>Название</th>") < html.index("<th>Артикул</th>")
    assert html.index("<th>Артикул</th>") < html.index("<th>Категория</th>")


def test_brp_part_shows_material_no(make_user, client, refs):
    _admin(make_user, client)
    brp = BrpCatalogPart.objects.create(
        material_no="417224204", part_desc="AXLE - 10MM",
        retail_price_usd=Decimal("10"), wholesale_price_usd=Decimal("7"),
    )
    promote_brp(brp)
    cell = _article_cell(_list_html(client, show="all"), "AXLE - 10MM")
    assert '<span class="code-pill">417224204</span>' in cell


def test_polaris_part_shows_part_number(make_user, client, refs):
    _admin(make_user, client)
    pol = PolarisCatalogPart.objects.create(
        part_number="3086878", part_name="BALL BEARING",
        wholesale_price_usd=Decimal("6"), retail_price_usd=Decimal("20"),
    )
    promote_polaris(pol)
    cell = _article_cell(_list_html(client, show="all"), "BALL BEARING")
    assert '<span class="code-pill">3086878</span>' in cell


def test_plain_part_shows_primary_oem(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Деталь с OEM")
    PartNumber.objects.create(part=part, value="OEM-777", kind=KIND.OEM, is_primary=True)
    cell = _article_cell(_list_html(client), "Деталь с OEM")
    assert '<span class="code-pill">OEM-777</span>' in cell


def test_plain_part_shows_article_when_no_oem(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Деталь с артикулом")
    PartNumber.objects.create(part=part, value="ART-555", kind=KIND.ARTICLE)
    cell = _article_cell(_list_html(client), "Деталь с артикулом")
    assert '<span class="code-pill">ART-555</span>' in cell


def test_analog_not_used_as_article(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Только аналог")
    PartNumber.objects.create(part=part, value="099-ANALOG", kind=KIND.ANALOG)
    cell = _article_cell(_list_html(client), "Только аналог")
    assert "099-ANALOG" not in cell
    assert "—" in cell


def test_internal_ref_not_used_as_article(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Только внутренний")
    PartNumber.objects.create(part=part, value="INT-001", kind=KIND.INTERNAL_REF)
    cell = _article_cell(_list_html(client), "Только внутренний")
    assert "INT-001" not in cell
    assert "—" in cell


def test_primary_internal_ref_does_not_beat_oem(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Внутренний и OEM")
    PartNumber.objects.create(part=part, value="INT-999", kind=KIND.INTERNAL_REF, is_primary=True)
    PartNumber.objects.create(part=part, value="OEM-333", kind=KIND.OEM)
    cell = _article_cell(_list_html(client), "Внутренний и OEM")
    assert '<span class="code-pill">OEM-333</span>' in cell
    assert "INT-999" not in cell


def test_missing_article_shows_dash(make_user, client, refs):
    _admin(make_user, client)
    _make_part(refs, name="Совсем без номера")
    cell = _article_cell(_list_html(client), "Совсем без номера")
    assert "—" in cell
    assert "code-pill" not in cell
    assert "None" not in cell


def test_ds_like_internal_number_not_shown_as_article(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Деталь с DS")
    # DS-номер экземпляра моделируем как INTERNAL_REF: в колонку артикула он не попадает.
    PartNumber.objects.create(part=part, value="DS-000123", kind=KIND.INTERNAL_REF)
    PartNumber.objects.create(part=part, value="OEM-100", kind=KIND.OEM, is_primary=True)
    cell = _article_cell(_list_html(client), "Деталь с DS")
    assert '<span class="code-pill">OEM-100</span>' in cell
    assert "DS-000123" not in cell


def test_search_by_exact_article_still_finds(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Искомая деталь")
    PartNumber.objects.create(part=part, value="XR-2024", kind=KIND.OEM)
    html = _list_html(client, q="XR-2024")
    assert "Искомая деталь" in html
    assert '<span class="code-pill">XR-2024</span>' in _article_cell(html, "Искомая деталь")


def test_search_by_brp_material_no_finds(make_user, client, refs):
    _admin(make_user, client)
    brp = BrpCatalogPart.objects.create(
        material_no="417224204", part_desc="AXLE FIND",
        retail_price_usd=Decimal("10"), wholesale_price_usd=Decimal("7"),
    )
    promote_brp(brp)
    html = _list_html(client, q="417224204", show="all")
    assert "AXLE FIND" in html


def test_search_by_analog_shows_canonical_article(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Найдена по аналогу")
    PartNumber.objects.create(part=part, value="AN-500", kind=KIND.ANALOG)
    PartNumber.objects.create(part=part, value="OEM-CANON", kind=KIND.OEM, is_primary=True)
    html = _list_html(client, q="AN-500")
    assert "Найдена по аналогу" in html
    cell = _article_cell(html, "Найдена по аналогу")
    assert '<span class="code-pill">OEM-CANON</span>' in cell
    assert "AN-500" not in cell


def test_active_filter_still_works_with_article(make_user, client, refs):
    _admin(make_user, client)
    active = _make_part(refs, name="Активная деталь")
    PartNumber.objects.create(part=active, value="OEM-ACT", kind=KIND.OEM)
    disabled = _make_part(refs, name="Отключённая деталь", is_active=False)
    PartNumber.objects.create(part=disabled, value="OEM-OFF", kind=KIND.OEM)
    active_html = _list_html(client, show="active")
    assert "Активная деталь" in active_html
    assert "Отключённая деталь" not in active_html
    all_html = _list_html(client, show="all")
    assert "Отключённая деталь" in all_html


def test_money_format_unchanged_in_article_row(make_user, client, refs):
    _admin(make_user, client)
    part = _make_part(refs, name="Деталь с ценой", recommended_price=Decimal("1616.00"))
    PartNumber.objects.create(part=part, value="OEM-PR", kind=KIND.OEM)
    cells = _row_cells(_list_html(client), "Деталь с ценой")
    money = " ".join(cells)
    assert "1 616" in money
    assert "1616.00" not in money


def test_parts_list_article_no_query_growth(make_user, client, refs):
    """SQL-запросы списка не растут линейно от числа деталей (нет N+1)."""
    _admin(make_user, client)
    brp = BrpCatalogPart.objects.create(
        material_no="500000001", part_desc="BRP ONE",
        retail_price_usd=Decimal("10"), wholesale_price_usd=Decimal("7"),
    )
    promote_brp(brp)
    with CaptureQueriesContext(connection) as first:
        client.get(reverse("part_list"), {"show": "all"})

    for i in range(2, 10):
        b = BrpCatalogPart.objects.create(
            material_no=f"50000000{i}", part_desc=f"BRP {i}",
            retail_price_usd=Decimal("10"), wholesale_price_usd=Decimal("7"),
        )
        promote_brp(b)
        plain = _make_part(refs, name=f"Обычная {i}")
        PartNumber.objects.create(part=plain, value=f"OEM-{i}", kind=KIND.OEM)
    with CaptureQueriesContext(connection) as many:
        client.get(reverse("part_list"), {"show": "all"})

    # 16 новых деталей не должны давать линейного роста запросов.
    assert len(many) <= len(first) + 3, (len(first), len(many))
