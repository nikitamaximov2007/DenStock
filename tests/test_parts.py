import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts import roles
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
    assert "Детали" in html
