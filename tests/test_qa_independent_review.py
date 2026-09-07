"""Независимая проверка ветки codex/next-product-package (QA-сессия).

Тесты написаны отдельной ревью-сессией и намеренно НЕ повторяют тесты
реализации. Каждый из них падает на 4fb187e и описывает поведение, которое
ветка обещает, но не выполняет. Продуктовая логика здесь не меняется.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.cart import add_scan, complete_cart, open_cart
from apps.actions.models import PartCustomsInfo, WarehouseAction
from apps.actions.services import ActionError, perform_action
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
Area = PartCustomsInfo.ApplicationArea


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
    return {"sup": sup, "admin": admin, "loc": loc, "part": part}


def _login(client, make_user, *, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


def _card(part, **overrides):
    values = {
        "customs_name_ru": "БОЛТ", "customs_name_ru_confirmed": True,
        "customs_name_en": "BOLT", "manufacturer": "BRP",
        "country_of_origin": "CANADA", "customs_unit_price_usd": Decimal("7"),
    }
    values.update(overrides)
    return PartCustomsInfo.objects.create(part_type=part, **values)


def _add(client, env, *, kind="sale", qty="1"):
    return client.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": kind, "quantity": qty, "q": "700100",
    })


def _row(client, env, *, kind="sale", **fields):
    payload = {
        "kind": kind, "operation": "set",
        "row_key": f"{env['part'].pk}:{env['loc'].pk}",
        "quantity": "1", "q": "700100",
    }
    payload.update(fields)
    return client.post(reverse("actions_cart_update"), payload)


def _complete(client, *, kind="sale", customer=None):
    customer = customer or Customer.objects.create(name="Иванов")
    return client.post(reverse("actions_cart_complete"), {
        "kind": kind, "customer_id": customer.pk, "q": "700100",
    }, follow=True)


# --- 1. Пара весов: брутто не может быть легче нетто -------------------------------------


def test_quick_action_rejects_net_heavier_than_gross(client, make_user, env):
    """Карточка детали это правило соблюдает (validate_weight_pair), быстрые
    действия - нет. Невозможная пара уходит в карточку и замораживается в
    таможенном заказе, после чего карточку уже не сохранить."""
    _login(client, make_user)
    _add(client, env)
    _row(client, env, gross_weight_g="10", net_weight_g="500", application_area="КАТЕР")
    _complete(client)
    customs = PartCustomsInfo.objects.filter(part_type=env["part"]).first()
    assert customs is None or not (
        customs.gross_weight_kg is not None
        and customs.net_weight_kg is not None
        and customs.gross_weight_kg < customs.net_weight_kg
    ), "быстрые действия записали брутто легче нетто"


# --- 2. Одиночное действие тоже создаёт таможенный источник ------------------------------


def test_single_scan_sale_requires_customs_metadata(env):
    """complete_cart отказывает без веса и области, а perform_action - нет.
    Обе дороги создают завершённую Sale, то есть каноническую строку выгрузки."""
    with pytest.raises(ActionError):
        perform_action(
            part=env["part"], location=env["loc"],
            action_type=WarehouseAction.Type.SALE,
            quantity="1", customer_comment="Иванов",
            scanned_number="700100", by=env["admin"],
        )


def test_single_scan_repair_requires_customs_metadata(env):
    with pytest.raises(ActionError):
        perform_action(
            part=env["part"], location=env["loc"],
            action_type=WarehouseAction.Type.REPAIR,
            quantity="1", customer_comment="Иванов",
            scanned_number="700100", by=env["admin"],
        )


# --- 3. Формат количества и цены в очереди таможни ---------------------------------------


def _sold(env, *, qty="1"):
    _card(env["part"], gross_weight_kg=Decimal("0.250"),
          net_weight_kg=Decimal("0.200"), application_area=Area.SNOWMOBILE)
    cart = open_cart("sale", by=env["admin"])
    add_scan(cart, env["part"], env["loc"], quantity=Decimal(qty), by=env["admin"])
    complete_cart(cart, customer_comment="Иванов", by=env["admin"])


def test_customs_queue_shows_whole_quantities(client, make_user, env):
    """Stage 3 обещает целые количества. В очереди отчёта Decimal(x,3)
    печатается как есть и в ru-локали выглядит как «1,000»."""
    _sold(env)
    _login(client, make_user)
    html = client.get(reverse("actions_report")).content.decode()
    queue = html.split("data-customs-source-table")[1].split("</table>")[0]
    assert "1,000" not in queue, "количество в очереди печатается с тремя знаками"


def test_selection_page_shows_whole_quantities(client, make_user, env):
    """Та же страница, на которой оператор выбирает границу заказа.

    Цена USD здесь приходит из каталога (decimal_places=2) и печатается верно;
    проблема именно в количестве."""
    _sold(env)
    _login(client, make_user)
    html = client.get(reverse("customs_order_selection")).content.decode()
    assert "1,000" not in html, "количество на странице выбора печатается с тремя знаками"


# --- 4. Пресет периода и остальные фильтры не должны вытеснять друг друга ----------------


def test_period_preset_link_keeps_the_other_filters(client, make_user, env):
    """Пресеты - это ссылки «?preset=X» внутри GET-формы. Клик по ним теряет
    тип/поиск/ячейку, а отправка формы теряет пресет."""
    _sold(env)
    _login(client, make_user)
    html = client.get(reverse("actions_report"), {"action_type": "sale"}).content.decode()
    toolbar = html.split('class="filter-toolbar"')[1].split("</form>")[0]
    assert 'href="?preset=today"' not in toolbar, (
        "ссылка пресета отбрасывает остальные фильтры"
    )
