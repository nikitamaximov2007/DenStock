"""Каждый экран, до которого можно дойти по меню, должен открываться.

Это не проверка содержимого, а проверка того, что ни одна страница не отвечает
ошибкой сервера и не показывает человеку кусок шаблона или слово из
трассировки.

Список экранов не выписан руками, а собран из самого меню: то, что видит
оператор, и то, что проверяется, - один и тот же набор. Новый пункт меню
попадает под проверку сам, а исчезнувший перестаёт проверяться вместе с
исчезновением из меню.

Склад для проверки заполняется настоящими службами: на пустой базе половина
экранов не показывает ни одной строки и прячет ровно те дефекты, ради которых
всё это и делается.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts import roles
from apps.accounts.navigation import navigation
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.catalog.services import create_manual_part
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "parol-12345"

DEVELOPER_WORDS = (
    "Traceback",
    "IntegrityError",
    "DoesNotExist",
    "KeyError",
    "NoReverseMatch",
    "ValidationError",
)
TEMPLATE_LEFTOVERS = ("{#", "{%", "{{")


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
def warehouse(db, make_user):
    """Небольшой правдоподобный склад: продажа, ремонт и ручная деталь."""
    admin = make_user("boss", is_superuser=True)
    supplier = Supplier.objects.create(name="ООО Поставка")
    unit = Unit.objects.get(name="Штука")
    category = Category.objects.create(name="Вариатор")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S02-D03-C01", storage_allowed=True, is_active=True
    )

    belt = PartType.objects.create(
        name="Ремень вариатора", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("6500"),
    )
    PartNumber.objects.create(
        part=belt, value="417300383", kind=PartNumber.Kind.OEM, is_primary=True
    )
    manual = create_manual_part(name="Пружина", article="ЗАП-77", price=Decimal("450"))

    lots = {}
    for part, quantity, cost in ((belt, "10", "3900"), (manual, "8", "260")):
        batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch, part_type=part,
            quantity=Decimal(quantity), unit_cost_currency=Decimal(cost),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, admin)
        line.refresh_from_db()
        lot = create_stock_lot(line, location, Decimal(quantity))
        receive_stock_lot(lot, by=admin)
        lots[part.name] = lot

    customer = Customer.objects.create(name="Иванов Пётр")
    sale = create_sale(customer=customer, customer_name="", by=admin)
    add_stock_lot_to_sale(
        sale, lots["Ремень вариатора"], Decimal("1"),
        unit_price=Decimal("6500"), by=admin,
    )
    complete_sale(sale, by=admin)

    order = create_repair_order(customer=customer, customer_name="", by=admin)
    add_stock_lot_to_repair_order(order, lots["Пружина"], Decimal("2"), by=admin)
    complete_repair_order(order, by=admin)

    return {"admin": admin, "customer": customer, "belt": belt, "manual": manual}


def menu_addresses(user) -> list[str]:
    """Все адреса из меню: верхние пункты, боковые группы и вкладки раздела.

    Меню строится для нескольких разделов сразу, потому что вкладки раздела
    зависят от текущего адреса: находясь на главной, вкладок склада не увидишь.
    """
    factory = RequestFactory()
    found = []
    starting_points = [
        "/", "/parts/", "/inventory/balance/", "/receipts/", "/sales/sales/",
        "/repairs/orders/", "/sales/customers/", "/reports/", "/warehouse/",
    ]
    for path in starting_points:
        request = factory.get(path)
        request.user = user
        context = navigation(request)
        for item in context["nav_items"]:
            found.append(item["url"])
        for group in context["nav_groups"]:
            for tab in group.get("tabs", []):
                found.append(tab["url"])
        for tab in context["section_tabs"] + context["section_subtabs"]:
            found.append(tab["url"])
    unique = sorted({url for url in found if url and url.startswith("/")})
    return unique


@pytest.fixture
def addresses(warehouse):
    found = menu_addresses(warehouse["admin"])
    assert len(found) > 15, f"меню отдало подозрительно мало адресов: {found}"
    return found


def test_the_menu_leads_somewhere(addresses):
    assert "/" in addresses
    assert any(url.startswith("/reports/") for url in addresses)
    assert any(url.startswith("/parts/") for url in addresses)


def test_no_screen_in_the_menu_answers_with_a_server_error(client, warehouse, addresses):
    client.login(username="boss", password=PASSWORD)
    broken = []
    for url in addresses:
        response = client.get(url)
        if response.status_code >= 500:
            broken.append(f"{url} -> {response.status_code}")
    assert not broken, f"экраны отвечают ошибкой сервера: {broken}"


def test_no_screen_in_the_menu_is_a_dead_link(client, warehouse, addresses):
    """Пункт меню, ведущий в никуда, - это тупик для оператора."""
    client.login(username="boss", password=PASSWORD)
    dead = []
    for url in addresses:
        response = client.get(url)
        if response.status_code == 404:
            dead.append(url)
    assert not dead, f"пункты меню ведут на несуществующие страницы: {dead}"


def test_no_screen_shows_a_piece_of_its_own_template(client, warehouse, addresses):
    client.login(username="boss", password=PASSWORD)
    leaking = []
    for url in addresses:
        response = client.get(url)
        if response.status_code != 200:
            continue
        body = response.content.decode()
        for leftover in TEMPLATE_LEFTOVERS:
            if leftover in body:
                leaking.append(f"{url}: «{leftover}»")
    assert not leaking, f"на страницах виден шаблон: {leaking}"


def test_no_screen_shows_a_word_from_a_traceback(client, warehouse, addresses):
    """Оператор не должен читать то, что написано для программиста."""
    client.login(username="boss", password=PASSWORD)
    slipped = []
    for url in addresses:
        response = client.get(url)
        if response.status_code != 200:
            continue
        body = response.content.decode()
        for word in DEVELOPER_WORDS:
            if word in body:
                slipped.append(f"{url}: «{word}»")
    assert not slipped, f"на страницах слова из трассировки: {slipped}"


def test_a_storekeeper_gets_an_answer_everywhere_the_menu_offers(client, warehouse, make_user):
    """Роль без права на деньги видит своё меню, и оно тоже должно работать."""
    keeper = make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    broken = []
    for url in menu_addresses(keeper):
        response = client.get(url)
        if response.status_code >= 500 or response.status_code == 404:
            broken.append(f"{url} -> {response.status_code}")
    assert not broken, f"кладовщику меню предлагает нерабочие экраны: {broken}"


# --- Экраны, куда попадают по ссылке из строки, а не из меню ------------------------


def test_the_client_screens_open_from_their_lists(client, warehouse):
    """До истории клиента доходят из списка клиентов, а не из меню."""
    client.login(username="boss", password=PASSWORD)
    customer = warehouse["customer"]
    for name in (
        "reports_client_timeline",
        "reports_repairs_by_client_detail",
        "customer_detail",
    ):
        if name == "customer_detail":
            url = reverse(name, args=[customer.pk])
            response = client.get(url)
        else:
            url = reverse(name)
            response = client.get(url, {"customer_id": customer.pk})
        assert response.status_code == 200, f"{name}: ответ {response.status_code}"


def test_a_client_screen_without_a_client_does_not_crash(client, warehouse):
    """Обрезанная ссылка - обычное дело. Ответ должен быть внятным."""
    client.login(username="boss", password=PASSWORD)
    for name in ("reports_client_timeline", "reports_repairs_by_client_detail"):
        response = client.get(reverse(name))
        assert response.status_code < 500, f"{name}: ответ {response.status_code}"


def test_wide_tables_scroll_by_themselves_on_a_phone():
    """Вторая денежная колонка не должна ломать телефон.

    Раскладку спасает не обёртка вокруг таблицы, а правило на самой таблице:
    на узком экране она становится блоком с собственной прокруткой. Если это
    правило убрать, вбок поедет вся страница вместе с шапкой и меню, и
    заметить это на большом мониторе будет невозможно.
    """
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    narrow = css.split("@media (max-width: 900px)")[1].split("@media")[0]
    assert "overflow-x: auto" in narrow, "широкие таблицы перестали прокручиваться"
    assert ".table { display: block" in narrow


def test_the_report_tables_are_ordinary_tables(client, warehouse):
    """Правило про телефон действует только на обычный класс таблицы.

    Если отчёт когда-нибудь отрисуют своей разметкой, он выпадет из этого
    правила молча.
    """
    client.login(username="boss", password=PASSWORD)
    for name in ("reports_client_timeline", "reports_repairs_by_client_detail"):
        body = client.get(
            reverse(name), {"customer_id": warehouse["customer"].pk}
        ).content.decode()
        assert '<table class="table"' in body, f"{name}: таблица без общего класса"


def test_a_part_that_does_not_exist_is_a_normal_404(client, warehouse):
    client.login(username="boss", password=PASSWORD)
    response = client.get(reverse("part_detail", args=[999999]))
    assert response.status_code == 404
    body = response.content.decode()
    for word in DEVELOPER_WORDS:
        assert word not in body
