"""Создание ячейки по S/D/C: форма только для ячейки и честные причины отказа."""

import threading

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockBalance, StockMovement
from apps.warehouse.addresses import create_location
from apps.warehouse.models import StorageLocation, StorageLocationAlias
from apps.warehouse.services import StorageLocationCreateError

L = StorageLocation.Level
PASSWORD = "pass-12345"


@pytest.fixture
def manager(django_user_model, client):
    user = django_user_model.objects.create_user(
        username="cell-admin", password=PASSWORD, is_superuser=True
    )
    client.login(username="cell-admin", password=PASSWORD)
    return user


def _post(client, **data):
    payload = {"rack_number": "1", "drawer_number": "5", "cell_number": "1", "description": ""}
    payload.update(data)
    return client.post(reverse("location_create"), payload)


def _rack(code="S01"):
    return StorageLocation.objects.create(
        name=code, code=code, barcode=f"LOC:{code}", level=L.RACK, storage_allowed=False
    )


# --- Регрессия: исходная ошибка «уже существует» для свободного адреса ----------


@pytest.mark.django_db
def test_free_address_is_created_even_when_browser_sends_the_old_type_field(manager, client):
    """До исправления: «Что создать» по умолчанию = Стеллаж, и форма собирала S01.

    Стеллаж 1 существует, поэтому любой адрес 1-x-y давал «уже существует».
    """
    _rack()
    response = _post(client, location_type="rack")
    assert response.status_code == 302, response.context["form"].errors
    cell = StorageLocation.objects.get(code="S01-D05-C01")
    assert cell.level == L.CELL and cell.is_active and cell.storage_allowed
    assert response["Location"] == reverse("location_detail", args=[cell.pk])
    assert cell.parent.code == "S01-D05" and cell.parent.parent.code == "S01"


# --- Форма --------------------------------------------------------------------


@pytest.mark.django_db
def test_form_shows_only_rack_drawer_cell_and_description(manager, client):
    response = client.get(reverse("location_create"))
    assert response.status_code == 200
    form = response.context["form"]
    assert list(form.fields) == ["rack_number", "drawer_number", "cell_number", "description"]
    html = response.content.decode()
    for hidden in ("Что создать", "Название", "Назначение", "Вместимость"):
        assert hidden not in html
    assert "Новая ячейка" in html and "Сохранить" in html


@pytest.mark.django_db
def test_internal_fields_get_the_model_defaults(manager, client):
    response = _post(client, rack_number="2", drawer_number="3", cell_number="4",
                     description="У окна")
    assert response.status_code == 302
    cell = StorageLocation.objects.get(code="S02-D03-C04")
    assert cell.name == "S02-D03-C04"
    assert cell.purpose == StorageLocation.Purpose.NORMAL
    assert cell.capacity is None
    assert cell.description == "У окна"
    assert cell.barcode == "LOC:S02-D03-C04"
    assert cell.sort_order == 4


@pytest.mark.django_db
def test_description_is_optional(manager, client):
    assert _post(client, description="").status_code == 302
    assert StorageLocation.objects.get(code="S01-D05-C01").description == ""


@pytest.mark.django_db
def test_drawer_zero_is_a_valid_cell_address(manager, client):
    assert _post(client, drawer_number="0", cell_number="2").status_code == 302
    assert StorageLocation.objects.filter(code="S01-D00-C02", level=L.CELL).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rack_number", ""),
        ("rack_number", "0"),
        ("rack_number", "-1"),
        ("drawer_number", ""),
        ("drawer_number", "-1"),
        ("cell_number", ""),
        ("cell_number", "0"),
        ("cell_number", "-3"),
        ("cell_number", "abc"),
    ],
)
@pytest.mark.django_db
def test_wrong_numbers_are_refused_and_nothing_is_created(manager, client, field, value):
    response = _post(client, **{field: value}, description="сохранить ввод")
    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert not StorageLocation.objects.exists()
    assert "сохранить ввод" in response.content.decode()  # введённое не теряется


# --- Существующие, архивные и старые адреса -------------------------------------


@pytest.mark.django_db
def test_existing_active_cell_is_not_duplicated(manager, client):
    create_location("S01-D05-C01")
    response = _post(client, description="второй раз")
    assert response.status_code == 200
    html = response.content.decode()
    assert "Ячейка 1-5-1 уже существует." in html
    assert "второй раз" in html
    assert StorageLocation.objects.filter(code="S01-D05-C01").count() == 1


@pytest.mark.django_db
def test_archived_cell_is_reported_as_archived_and_never_overwritten(manager, client):
    """Как на production 2026-10-12: 1-5-1 и ящик 1-5 отключены (архив)."""
    cell = create_location("S01-D05-C01", description="история")
    drawer = cell.parent
    StorageLocation.objects.filter(pk__in=[cell.pk, drawer.pk]).update(
        is_active=False, storage_allowed=False
    )
    response = _post(client, description="новая")
    assert response.status_code == 200
    html = response.content.decode()
    assert "Ячейка 1-5-1 уже есть, но отключена (в архиве)" in html
    assert "Активировать" in html and "1-5" in html
    assert reverse("location_detail", args=[cell.pk]) in html
    cell.refresh_from_db()
    assert not cell.is_active and cell.description == "история"
    assert StorageLocation.objects.filter(code="S01-D05-C01").count() == 1


@pytest.mark.django_db
def test_new_cell_under_an_archived_drawer_names_the_drawer(manager, client):
    cell = create_location("S01-D05-C01")
    StorageLocation.objects.filter(pk=cell.parent.pk).update(is_active=False)
    response = _post(client, cell_number="2")
    assert response.status_code == 200
    assert "Ящик 1-5 отключён (в архиве)" in response.content.decode()
    assert not StorageLocation.objects.filter(code="S01-D05-C02").exists()


@pytest.mark.django_db
def test_old_address_of_another_cell_is_refused(manager, client):
    owner = create_location("S02-D01-C01")
    StorageLocationAlias.objects.create(
        location=owner, code="S01-D05-C01", kind=StorageLocationAlias.Kind.RENAME
    )
    response = _post(client)
    assert response.status_code == 200
    assert "старый адрес ячейки 2-1-1" in response.content.decode()
    assert not StorageLocation.objects.filter(code="S01-D05-C01").exists()


@pytest.mark.django_db
def test_missing_rack_and_drawer_are_created_as_before(manager, client):
    assert _post(client, rack_number="7", drawer_number="2", cell_number="3").status_code == 302
    assert StorageLocation.objects.get(code="S07").level == L.RACK
    assert StorageLocation.objects.get(code="S07-D02").level == L.DRAWER


@pytest.mark.django_db
def test_repeated_post_creates_one_cell(manager, client):
    assert _post(client).status_code == 302
    second = _post(client)
    assert second.status_code == 200
    assert StorageLocation.objects.filter(code="S01-D05-C01").count() == 1


@pytest.mark.django_db
def test_created_cell_is_listed_in_the_warehouse_structure(manager, client):
    _post(client)
    html = client.get(reverse("warehouse_index")).content.decode()
    assert "1-5-1" in html and "Добавить ячейку" in html


@pytest.mark.django_db
def test_creating_a_cell_does_not_touch_existing_places_or_stock(manager, client):
    other = create_location("S03-D01-C01", description="старая")
    unit = Unit.objects.get_or_create(name="Штука")[0]
    part = PartType.objects.create(
        name="Деталь", category=Category.objects.create(name="Кат"), unit=unit
    )
    before_places = list(StorageLocation.objects.values().order_by("pk"))
    before_stock = list(StockBalance.objects.values().order_by("pk"))
    before_movements = StockMovement.objects.count()
    assert _post(client).status_code == 302
    existing_ids = [row["id"] for row in before_places]
    after_places = list(StorageLocation.objects.filter(pk__in=existing_ids).values().order_by("pk"))
    assert after_places == before_places
    assert list(StockBalance.objects.values().order_by("pk")) == before_stock
    assert StockMovement.objects.count() == before_movements
    assert part.pk and other.pk


@pytest.mark.django_db
def test_storekeeper_cannot_open_or_post_the_form(django_user_model, client):
    user = django_user_model.objects.create_user(username="sklad-cells", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    client.login(username="sklad-cells", password=PASSWORD)
    assert client.get(reverse("location_create")).status_code in {302, 403}
    _post(client)
    assert not StorageLocation.objects.exists()


# --- Конкурентное создание (PostgreSQL 16) ---------------------------------------


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_creation_of_the_same_cell_yields_one_row():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL required")
    barrier = threading.Barrier(4)
    outcomes = []

    def worker():
        from django.db import connections

        try:
            barrier.wait()
            create_location("S04-D02-C09")
            outcomes.append("created")
        except StorageLocationCreateError as exc:
            outcomes.append(str(exc))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("created") == 1, outcomes
    assert all(o == "created" or o == "Ячейка 4-2-9 уже существует." for o in outcomes), outcomes
    assert StorageLocation.objects.filter(code="S04-D02-C09").count() == 1
    assert StorageLocation.objects.filter(code="S04-D02").count() == 1
    assert StorageLocation.objects.filter(code="S04").count() == 1
