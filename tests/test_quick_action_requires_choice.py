"""Stage 5 — действие в быстрых действиях сотрудник выбирает сам.

Раньше в списке заранее стояла «Продажа»: первый же скан по невнимательности
списывал товар и создавал проведённый документ. Теперь действие остаётся
невыбранным, а попытка провести без выбора отклоняется на сервере - не только
атрибутом required в разметке.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.cart import cart_rows
from apps.actions.models import PartCustomsInfo, WarehouseAction
from apps.actions.services import perform_action, stock_overview
from apps.actions.views import ACTION_REQUIRED_MESSAGE, _eligible_action_locations
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
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


def _stock(part, location, qty, sup, admin):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(str(qty)), unit_cost_currency=Decimal("1")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def env(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D03-C08", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Болт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value="700100", kind=PartNumber.Kind.OEM)
    _stock(part, loc, 10, sup, admin)
    PartCustomsInfo.objects.create(
        part_type=part, gross_weight_kg=Decimal("0.350"), net_weight_kg=Decimal("0.300"),
        application_area=PartCustomsInfo.ApplicationArea.SNOWMOBILE,
    )
    return {"sup": sup, "admin": admin, "loc": loc, "part": part}


def _login(client, make_user, *, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


# --- Значение по умолчанию ---------------------------------------------------------------


def test_action_is_not_preselected(client, make_user, env):
    _login(client, make_user)
    html = client.get(reverse("actions_scan")).content.decode()
    assert '<option value="" selected>Не выбрано</option>' in html


def test_sale_is_not_selected_by_default(client, make_user, env):
    _login(client, make_user)
    html = client.get(reverse("actions_scan")).content.decode()
    sale = html.split('value="sale"', 1)[1].split(">", 1)[0]
    assert "selected" not in sale


def test_a_chosen_action_stays_chosen(client, make_user, env):
    _login(client, make_user)
    html = client.get(reverse("actions_scan") + "?kind=repair").content.decode()
    repair = html.split('value="repair"', 1)[1].split(">", 1)[0]
    assert "selected" in repair
    assert '<option value="" selected>' not in html


# --- Отказ на сервере --------------------------------------------------------------------


def test_scan_without_an_action_is_refused(client, make_user, env):
    _login(client, make_user)
    resp = client.post(
        reverse("actions_cart_scan"), {"kind": "", "q": "700100"}, follow=True
    )
    assert ACTION_REQUIRED_MESSAGE in resp.content.decode()
    assert not Sale.objects.exists()
    assert not WarehouseAction.objects.exists()


def test_scan_without_an_action_changes_no_stock(client, make_user, env):
    _login(client, make_user)
    movements = StockMovement.objects.count()
    client.post(reverse("actions_cart_scan"), {"kind": "", "q": "700100"}, follow=True)
    assert StockMovement.objects.count() == movements


def test_cart_add_without_an_action_is_refused(client, make_user, env):
    _login(client, make_user)
    resp = client.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": "", "quantity": "1", "q": "700100",
    }, follow=True)
    assert ACTION_REQUIRED_MESSAGE in resp.content.decode()
    assert not Sale.objects.exists()


def test_perform_without_an_action_is_refused(client, make_user, env):
    _login(client, make_user)
    resp = client.post(reverse("actions_perform"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": "", "quantity": "1", "q": "700100",
        "customer_comment": "Иванов",
    }, follow=True)
    assert ACTION_REQUIRED_MESSAGE in resp.content.decode()
    assert not WarehouseAction.objects.exists()


def test_completing_a_cart_without_a_kind_is_refused(client, make_user, env):
    _login(client, make_user)
    resp = client.post(reverse("actions_cart_complete"), {
        "kind": "", "customer_id": Customer.objects.create(name="Иванов").pk, "q": "700100",
    }, follow=True)
    assert ACTION_REQUIRED_MESSAGE in resp.content.decode()
    assert not WarehouseAction.objects.exists()


# --- Существующие потоки не сломаны ------------------------------------------------------


def _add(client, env, kind):
    return client.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": kind, "quantity": "1", "q": "700100",
    })


def _complete(client, kind):
    return client.post(reverse("actions_cart_complete"), {
        "kind": kind, "customer_id": Customer.objects.create(name="Иванов").pk, "q": "700100",
    }, follow=True)


def test_sale_still_works_when_chosen(client, make_user, env):
    _login(client, make_user)
    _add(client, env, "sale")
    _complete(client, "sale")
    assert Sale.objects.filter(status=Sale.Status.COMPLETED).count() == 1
    assert WarehouseAction.objects.filter(action_type=WarehouseAction.Type.SALE).count() == 1


def test_repair_still_works_when_chosen(client, make_user, env):
    _login(client, make_user)
    _add(client, env, "repair")
    _complete(client, "repair")
    assert RepairOrder.objects.filter(status=RepairOrder.Status.COMPLETED).count() == 1
    assert WarehouseAction.objects.filter(action_type=WarehouseAction.Type.REPAIR).count() == 1


def test_reserve_still_works_when_chosen(client, make_user, env):
    _login(client, make_user)
    resp = client.post(reverse("actions_perform"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": "reserve", "quantity": "1", "q": "700100",
        "customer_comment": "Иванов", "request_token": "tok-reserve-1",
    }, follow=True)
    assert resp.status_code == 200
    assert WarehouseAction.objects.filter(action_type=WarehouseAction.Type.RESERVE).count() == 1


def test_scanning_with_a_chosen_action_still_fills_the_cart(client, make_user, env):
    _login(client, make_user)
    client.post(reverse("actions_cart_scan"), {"kind": "sale", "q": "700100"}, follow=True)
    html = client.get(reverse("actions_scan")).content.decode()
    assert "Корзина · Продажа" in html


# --- Выбор ячейки по доступному остатку -----------------------------------------------


def test_one_eligible_cell_is_hidden_and_forged_location_is_ignored(client, make_user, env):
    other = StorageLocation.objects.create(
        name="Другая ячейка", code="S02-D03-C08", storage_allowed=True, is_active=True
    )
    _login(client, make_user)

    html = client.get(reverse("actions_scan") + "?q=700100&kind=sale").content.decode()
    assert "Ячейка списания" not in html
    assert "Добавить выбранную ячейку" not in html

    response = client.post(
        reverse("actions_cart_add"),
        {
            "part_id": env["part"].pk,
            "location_id": other.pk,
            "action_type": "sale",
            "quantity": "1",
            "q": "700100",
        },
        follow=True,
    )
    assert "В корзину «Продажа»" in response.content.decode()
    cart = Sale.objects.get(status=Sale.Status.DRAFT)
    assert cart_rows(cart)[0].location.pk == env["loc"].pk

    client.post(
        reverse("actions_perform"),
        {
            "part_id": env["part"].pk,
            "location_id": other.pk,
            "action_type": "sale",
            "quantity": "1",
            "customer_comment": "Клиент",
            "q": "700100",
        },
    )
    action = WarehouseAction.objects.get(action_type=WarehouseAction.Type.SALE)
    assert action.location_id == env["loc"].pk


def test_multiple_lots_in_one_cell_still_hide_location_choice(client, make_user, env):
    _stock(env["part"], env["loc"], 3, env["sup"], env["admin"])
    _login(client, make_user)

    html = client.get(reverse("actions_scan") + "?q=700100&kind=sale").content.decode()
    assert "Ячейка списания" not in html
    eligible_ids = [row["location"].pk for row in _eligible_action_locations(env["part"])]
    assert eligible_ids == [env["loc"].pk]


def test_fully_reserved_cell_is_not_eligible_and_cannot_override_the_only_choice(
    client, make_user, env
):
    reserved = StorageLocation.objects.create(
        name="Зарезервированная ячейка", code="S02-D03-C08", storage_allowed=True, is_active=True
    )
    _stock(env["part"], reserved, 2, env["sup"], env["admin"])
    perform_action(
        part=env["part"],
        location=reserved,
        action_type=WarehouseAction.Type.RESERVE,
        quantity="2",
        customer_comment="Клиент",
        by=env["admin"],
    )
    _login(client, make_user)

    reserved_row = next(
        row for row in stock_overview(env["part"])["locations"] if row["location"] == reserved
    )
    assert reserved_row["available"] == 0
    html = client.get(reverse("actions_scan") + "?q=700100&kind=sale").content.decode()
    assert "Ячейка списания" not in html
    assert reserved.short_code not in html

    client.post(
        reverse("actions_cart_add"),
        {
            "part_id": env["part"].pk,
            "location_id": reserved.pk,
            "action_type": "sale",
            "quantity": "1",
            "q": "700100",
        },
    )
    cart = Sale.objects.get(status=Sale.Status.DRAFT)
    assert cart_rows(cart)[0].location.pk == env["loc"].pk


def test_multiple_eligible_cells_require_an_explicit_choice(client, make_user, env):
    other = StorageLocation.objects.create(
        name="Вторая ячейка", code="S02-D03-C08", storage_allowed=True, is_active=True
    )
    _stock(env["part"], other, 1, env["sup"], env["admin"])
    _login(client, make_user)

    html = client.get(reverse("actions_scan") + "?q=700100&kind=sale").content.decode()
    assert "Ячейка списания" in html
    assert 'name="location_id"' in html

    response = client.post(
        reverse("actions_cart_add"),
        {"part_id": env["part"].pk, "action_type": "sale", "quantity": "1", "q": "700100"},
        follow=True,
    )
    assert "Выберите ячейку списания." in response.content.decode()
