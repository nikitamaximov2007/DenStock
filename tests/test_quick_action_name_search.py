"""Operator search in Quick Actions: articles, names and live availability."""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.models import PartCustomsInfo
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Sale
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


def _stock(part, location, quantity, supplier, admin):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal("1"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(quantity))
    receive_stock_lot(lot, by=admin)


def _part(category, unit, *, name, article):
    part = PartType.objects.create(
        name=name,
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value=article, kind=PartNumber.Kind.OEM)
    return part


@pytest.fixture
def data(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Трансмиссия")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    shaft = _part(category, unit, name="Drive Shaft", article="ABC-001-9")
    housing = _part(category, unit, name="Drive Housing", article="ABC-002-8")
    belt = _part(category, unit, name="Drive Belt", article="ABC-003-7")
    _stock(shaft, location, "3", supplier, admin)
    PartCustomsInfo.objects.create(
        part_type=shaft, customs_name_ru="ВАЛ ПРИВОДНОЙ", customs_name_ru_confirmed=True
    )
    PartCustomsInfo.objects.create(
        part_type=housing, customs_name_ru="КОРПУС ПРИВОДА", customs_name_ru_confirmed=True
    )
    PartCustomsInfo.objects.create(
        part_type=belt, customs_name_ru="РЕМЕНЬ ПРИВОДА", customs_name_ru_confirmed=False
    )
    return {"shaft": shaft, "housing": housing, "belt": belt, "location": location}


def _login(client, make_user):
    make_user("operator", is_superuser=True)
    client.login(username="operator", password=PASSWORD)


def _screen(client, query):
    return client.get(reverse("actions_scan"), {"q": query, "kind": "sale"}).content.decode()


def test_exact_and_normalized_article_open_the_part(client, make_user, data):
    _login(client, make_user)
    for query in ("ABC-001-9", "abc0019"):
        html = _screen(client, query)
        assert "Drive Shaft" in html
        assert "Нет в наличии" not in html


def test_partial_article_and_name_show_a_deterministic_part_chooser(client, make_user, data):
    _login(client, make_user)
    for query in ("ABC", "drive"):
        html = _screen(client, query)
        assert "Drive Shaft" in html and "Drive Housing" in html
        assert "Русское название" in html
        assert "Артикул" in html
        assert "Производитель" in html
        assert "Доступно" in html
        assert 'name="part_id"' in html


def test_exact_and_partial_confirmed_russian_name_find_the_part(client, make_user, data):
    _login(client, make_user)
    assert "Drive Shaft" in _screen(client, "ВАЛ ПРИВОДНОЙ")
    html = _screen(client, "ПРИВОД")
    assert "Drive Shaft" in html and "Drive Housing" in html
    assert "Деталь не найдена" in _screen(client, "РЕМЕНЬ ПРИВОДА")


def test_unique_name_scan_adds_the_sole_eligible_cell_to_the_cart(client, make_user, data):
    _login(client, make_user)
    response = client.post(
        reverse("actions_cart_scan"), {"kind": "sale", "q": "drive shaft"}, follow=True
    )
    assert "Добавлено: Drive Shaft" in response.content.decode()
    sale = Sale.objects.get(status=Sale.Status.DRAFT)
    assert sale.lines.get().stock_lot.location_id == data["location"].pk


def test_zero_stock_part_is_searchable_but_cannot_be_added(client, make_user, data):
    _login(client, make_user)
    html = _screen(client, "Drive Housing")
    assert "Drive Housing" in html
    assert "Нет в наличии" in html

    response = client.post(
        reverse("actions_cart_scan"), {"kind": "sale", "q": "Drive Housing"}, follow=True
    )
    assert "Нет в наличии" in response.content.decode()
    assert not Sale.objects.exists()


def test_chooser_selection_uses_the_chosen_part_not_an_arbitrary_first(client, make_user, data):
    _login(client, make_user)
    response = client.post(
        reverse("actions_cart_scan"),
        {"kind": "sale", "q": "Drive", "part_id": data["shaft"].pk},
        follow=True,
    )
    assert "Добавлено: Drive Shaft" in response.content.decode()
    assert Sale.objects.get(status=Sale.Status.DRAFT).lines.get().part_type_id == data["shaft"].pk
