import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts import roles
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
L = StorageLocation.Level
P = StorageLocation.Purpose


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


def _create_payload(**over):
    data = {
        "name": "Место",
        "code": "LOC-1",
        "level": L.CELL,
        "purpose": P.NORMAL,
        "sort_order": 0,
        "storage_allowed": "on",
    }
    data.update(over)
    return data


def test_create_warehouse(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    resp = client.post(
        reverse("location_create"),
        _create_payload(name="Основной склад", code="СКЛАД-1", level=L.WAREHOUSE),
    )
    assert resp.status_code == 302
    assert StorageLocation.objects.filter(code="СКЛАД-1", level=L.WAREHOUSE).exists()


def test_create_zone_inside_warehouse(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    wh = StorageLocation.objects.create(name="Склад", code="СКЛАД-1", level=L.WAREHOUSE)
    resp = client.post(
        reverse("location_create"),
        _create_payload(name="Зона A", code="A", level=L.ZONE, parent=wh.pk),
    )
    assert resp.status_code == 302
    zone = StorageLocation.objects.get(code="A")
    assert zone.parent == wh


def test_create_nested_levels(db):
    wh = StorageLocation.objects.create(name="Склад", code="СКЛАД-1", level=L.WAREHOUSE)
    zone = StorageLocation.objects.create(name="A", code="A", level=L.ZONE, parent=wh)
    rack = StorageLocation.objects.create(name="03", code="03", level=L.RACK, parent=zone)
    shelf = StorageLocation.objects.create(name="02", code="02", level=L.SHELF, parent=rack)
    cell = StorageLocation.objects.create(name="04", code="04", level=L.CELL, parent=shelf)
    assert cell.parent == shelf
    assert rack.level == L.RACK


def test_full_address_built(db):
    wh = StorageLocation.objects.create(name="Склад", code="СКЛАД-1", level=L.WAREHOUSE)
    zone = StorageLocation.objects.create(name="A", code="A", level=L.ZONE, parent=wh)
    cell = StorageLocation.objects.create(name="03", code="03", level=L.CELL, parent=zone)
    assert cell.full_path == "СКЛАД-1 / A / 03"


def test_barcode_generated(db):
    loc = StorageLocation.objects.create(name="Ячейка", code="A-03-02", level=L.CELL)
    assert loc.barcode == "LOC:A-03-02"


def test_code_is_unique(db):
    StorageLocation.objects.create(name="Первая", code="DUP", level=L.CELL)
    dup = StorageLocation(name="Вторая", code="DUP", level=L.CELL, purpose=P.NORMAL)
    with pytest.raises(ValidationError):
        dup.full_clean()


def test_parent_cycle_rejected(db):
    a = StorageLocation.objects.create(name="A", code="A", level=L.ZONE)
    b = StorageLocation.objects.create(name="B", code="B", level=L.RACK, parent=a)
    a.parent = b
    with pytest.raises(ValidationError):
        a.full_clean()


def test_toggle_deactivates_without_delete(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    loc = StorageLocation.objects.create(name="Ячейка", code="C1", level=L.CELL)
    resp = client.post(reverse("location_toggle", args=[loc.pk]))
    assert resp.status_code == 302
    loc.refresh_from_db()
    assert loc.is_active is False
    assert StorageLocation.objects.filter(pk=loc.pk).exists()


def test_purpose_independent_of_level(db):
    zone = StorageLocation.objects.create(
        name="Приёмка", code="RCV", level=L.ZONE, purpose=P.RECEIVING
    )
    cell = StorageLocation.objects.create(
        name="Карантин", code="QRN", level=L.CELL, purpose=P.QUARANTINE
    )
    assert zone.level == L.ZONE and zone.purpose == P.RECEIVING
    assert cell.level == L.CELL and cell.purpose == P.QUARANTINE


def test_cannot_deactivate_with_active_children(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    wh = StorageLocation.objects.create(name="Склад", code="СКЛАД-1", level=L.WAREHOUSE)
    StorageLocation.objects.create(name="A", code="A", level=L.ZONE, parent=wh)
    resp = client.post(reverse("location_toggle", args=[wh.pk]))
    assert resp.status_code == 302
    wh.refresh_from_db()
    assert wh.is_active is True  # деактивация заблокирована


def test_storekeeper_cannot_create(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("location_create"), _create_payload(code="X1"))
    assert resp.status_code == 403
    assert not StorageLocation.objects.filter(code="X1").exists()


def test_viewer_cannot_edit(make_user, client):
    make_user("nabl", role=roles.VIEWER)
    client.login(username="nabl", password=PASSWORD)
    loc = StorageLocation.objects.create(name="Ячейка", code="C9", level=L.CELL)
    resp = client.post(reverse("location_edit", args=[loc.pk]), _create_payload(code="C9"))
    assert resp.status_code == 403


def test_storekeeper_can_view(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    loc = StorageLocation.objects.create(name="Ячейка", code="C2", level=L.CELL)
    assert client.get(reverse("warehouse_index")).status_code == 200
    assert client.get(reverse("location_detail", args=[loc.pk])).status_code == 200


def test_navigation_shows_warehouse(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("dashboard")).content.decode()
    assert "Склад" in html
