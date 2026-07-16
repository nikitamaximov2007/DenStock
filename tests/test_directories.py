import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import (
    Category,
    Manufacturer,
    VehicleMake,
    VehicleModel,
    VehicleType,
)
from apps.suppliers.models import Supplier

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


# --- Создание справочников (через экраны, под админом) ---
def test_create_category(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    resp = client.post(reverse("category_create"), {"name": "Двигатель", "sort_order": 0})
    assert resp.status_code == 302
    assert Category.objects.filter(name="Двигатель").exists()


def test_create_manufacturer(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    resp = client.post(reverse("manufacturer_create"), {"name": "Yamaha", "country": "Япония"})
    assert resp.status_code == 302
    assert Manufacturer.objects.filter(name="Yamaha").exists()


def test_create_vehicle_chain(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    vtype = VehicleType.objects.create(name="Гидроцикл")
    make = VehicleMake.objects.create(vehicle_type=vtype, name="Sea-Doo")
    model = VehicleModel.objects.create(vehicle_make=make, name="GTX", year_from=2020)
    assert model.vehicle_make.vehicle_type == vtype


def test_create_supplier(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    resp = client.post(
        reverse("supplier_create"),
        {"name": "ООО Поставка", "default_currency": "RUB"},
    )
    assert resp.status_code == 302
    assert Supplier.objects.filter(name="ООО Поставка").exists()


# --- Дерево категорий ---
def test_category_cycle_is_rejected(db):
    a = Category.objects.create(name="A")
    b = Category.objects.create(name="B", parent=a)
    a.parent = b  # попытка создать цикл A -> B -> A
    with pytest.raises(ValidationError):
        a.full_clean()


# --- Деактивация вместо удаления ---
def test_toggle_deactivates_without_delete(make_user, client):
    make_user("admin", is_superuser=True)
    client.login(username="admin", password=PASSWORD)
    man = Manufacturer.objects.create(name="BRP")
    resp = client.post(reverse("manufacturer_toggle", args=[man.pk]))
    assert resp.status_code == 302
    man.refresh_from_db()
    assert man.is_active is False
    assert Manufacturer.objects.filter(pk=man.pk).exists()


# --- Доступ ---
def test_storekeeper_cannot_edit(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("manufacturer_create"), {"name": "X"})
    assert resp.status_code == 403
    assert not Manufacturer.objects.filter(name="X").exists()


def test_viewer_cannot_edit(make_user, client):
    make_user("nabl", role=roles.VIEWER)
    client.login(username="nabl", password=PASSWORD)
    resp = client.post(reverse("manufacturer_create"), {"name": "Y"})
    assert resp.status_code == 403


def test_storekeeper_can_view(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    assert client.get(reverse("manufacturer_list")).status_code == 200
    assert client.get(reverse("category_list")).status_code == 200
    assert client.get(reverse("directory_index")).status_code == 200


def test_manager_can_edit(make_user, client):
    make_user("ruk", role=roles.MANAGER)
    client.login(username="ruk", password=PASSWORD)
    resp = client.post(reverse("manufacturer_create"), {"name": "Polaris", "country": "США"})
    assert resp.status_code == 302
    assert Manufacturer.objects.filter(name="Polaris").exists()


def test_navigation_shows_directories(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("dashboard")).content.decode()
    assert ">Каталог<" not in html
    assert "Справочники" not in html
    assert client.get(reverse("directory_index")).status_code == 200
