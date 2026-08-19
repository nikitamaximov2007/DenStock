"""Ежедневные экраны отвечают на рабочие вопросы, а не показывают устройство системы.

Проверяется два разных утверждения сразу. Первое: с рабочего экрана убрано то,
что не помогает ответить «что за деталь, сколько её, где лежит, почём». Второе,
не менее важное: убранное не уничтожено и по-прежнему доступно там, где ведут
расследование.

Упрощение, которое стирает данные, это не упрощение, а потеря.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockLot
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
NUMBER = "3211173"


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
    return make_user("boss", is_superuser=True)


@pytest.fixture
def stock(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Ремень вариатора", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("12400"),
    )
    PartNumber.objects.create(part=part, value=NUMBER, kind=PartNumber.Kind.OEM)

    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("10"),
        unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("10"))
    receive_stock_lot(lot, by=admin)
    return {"part": part, "lot": lot, "location": location, "batch": batch, "admin": admin}


def _login(client, user):
    client.login(username=user.username, password=PASSWORD)


def _search(client, query=NUMBER):
    return client.get(reverse("part_search"), {"q": query})


# --- Поиск: ячейки и партии не дублируются текстом над таблицей ----------------------------


def test_search_answers_the_five_working_questions(stock, client, admin):
    """Артикул, название, остаток, ячейка и цена обязаны быть на экране."""
    _login(client, admin)
    body = _search(client).content.decode()

    assert NUMBER in body, "нет артикула"
    assert "Ремень вариатора" in body, "нет названия"
    assert "Доступно" in body, "нет остатка"
    assert "S01-D01-C01" in body, "нет ячейки"
    assert "12 400" in body or "12400" in body, "нет цены"


def test_search_does_not_repeat_the_cells_as_plain_text(stock, client, admin):
    """Список ячеек строкой дублировал таблицу ячеек прямо под собой.

    Таблица строго полезнее: она показывает те же ячейки и сколько в каждой.
    """
    _login(client, admin)
    body = _search(client).content.decode()

    assert "Ячейки:" not in body, "список ячеек снова дублирует таблицу"
    # Таблица ячеек осталась и отвечает на вопрос лучше: она показывает не
    # только где лежит, но и сколько лежит в каждой ячейке.
    assert "S01-D01-C01" in body, "таблица ячеек исчезла вместе с дублем"
    assert "Доступно" in body


def test_search_does_not_show_batch_identifiers_in_the_summary(stock, client, admin):
    """Номер партии это внутренняя прослеживаемость, а не ответ на рабочий вопрос."""
    _login(client, admin)
    body = _search(client).content.decode()

    assert "Партии:" not in body, "номера партий вернулись в сводку поиска"


def test_the_batch_link_still_exists_where_investigations_happen(stock, client, admin):
    """Убрано с экрана поиска, но не из системы: партия открывается и полна."""
    _login(client, admin)
    batch = stock["batch"]

    resp = client.get(reverse("batch_detail", args=[batch.pk]))
    assert resp.status_code == 200
    assert stock["part"].name in resp.content.decode()

    lot = StockLot.objects.get(pk=stock["lot"].pk)
    assert lot.batch_id == batch.pk, "потеряна связь лота с партией"
    assert lot.location_id == stock["location"].pk, "потеряна связь лота с ячейкой"


# --- Ремонт: себестоимость выданного не должна читаться как сумма ремонта -------------------


def test_repair_list_names_the_cost_for_what_it_is(stock, client, admin):
    """«Себестоимость» без уточнения читается как сумма, которую платит клиент.

    У ремонта клиентской суммы система не хранит вовсе: cost_total это стоимость
    выданных со склада деталей. Подпись обязана говорить именно это, иначе
    руководитель примет складскую себестоимость за выручку.
    """
    _login(client, admin)
    order = create_repair_order(customer=None, customer_name="Иванов", by=admin)
    add_stock_lot_to_repair_order(order, stock["lot"], Decimal("2"), by=admin)
    complete_repair_order(order, by=admin)

    body = client.get(reverse("repair_order_list")).content.decode()
    headers = re.findall(r"<th[^>]*>(.*?)</th>", body, re.S)
    headers = [re.sub(r"<[^>]+>", "", h).strip() for h in headers]

    assert "Себестоимость выданного (₽)" in headers, (
        f"подпись не называет величину своим именем: {headers}"
    )
    assert "Себестоимость (₽)" not in headers


def test_the_repair_cost_field_itself_is_untouched(stock, admin):
    """Исправлена подпись, а не учёт: поле и его значение прежние."""
    from apps.repairs.models import RepairOrder

    order = create_repair_order(customer=None, customer_name="Иванов", by=admin)
    add_stock_lot_to_repair_order(order, stock["lot"], Decimal("2"), by=admin)
    order = complete_repair_order(order, by=admin)

    assert RepairOrder._meta.get_field("cost_total").verbose_name == "Себестоимость выданного (₽)"
    assert order.cost_total > 0, "себестоимость выданного перестала считаться"


# --- Роли: денежные величины закрыты правом, а не разметкой ---------------------------------


@pytest.mark.parametrize("role", [roles.SELLER, roles.STOREKEEPER])
def test_an_operator_without_the_money_right_never_sees_cost(
    stock, client, make_user, role
):
    """Сокрытие держится на праве, а не на том, что подпись убрали из шаблона."""
    user = make_user(f"user-{role}", role=role)
    _login(client, user)

    body = _search(client).content.decode()
    assert "Себестоимость" not in body, f"роль «{role}» видит закупочную себестоимость"


# --- Быстрые действия: один экран, одно пояснение ------------------------------------------


def test_quick_actions_explains_the_flow_once(stock, client, admin):
    """Вокруг сканера было два пересекающихся пояснения, одно над и одно под.

    Оба говорили «сканируйте в черновик». На телефоне это отодвигало главный
    ввод смены вниз. Осталось одно пояснение, и оно сохранило оба факта: что
    делает скан и что остаток меняется только при проведении.
    """
    _login(client, admin)
    body = client.get(reverse("actions_scan")).content.decode()

    assert body.count("черновик") == 1, "пояснение про черновик снова повторяется"
    assert "остаток изменится только" in body.lower(), (
        "потеряно предупреждение, что остаток меняется только при проведении"
    )
    assert "нескольких ячейках" in body, "потеряна подсказка про выбор ячейки"


def test_the_scanner_is_the_first_input_of_the_screen(stock, client, admin):
    """Сканер обязан оставаться главным вводом, а не одним из полей."""
    _login(client, admin)
    body = client.get(reverse("actions_scan")).content.decode()

    assert "data-scan-input" in body
    assert "autofocus" in body, "сканер потерял автофокус"
    # Ввод со сканера не должен исправляться телефоном.
    for guard in ('autocorrect="off"', 'autocapitalize="off"', 'spellcheck="false"'):
        assert guard in body, f"потеряна защита ввода сканера: {guard}"


def test_the_narrow_screen_gives_the_scanner_its_own_row():
    """На телефоне выбор действия и кнопка сжимали поле сканера вдвое."""
    import pathlib

    css = pathlib.Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
    text = css.read_text(encoding="utf-8")
    rule = re.search(
        r"@media \(max-width: 560px\)\s*\{.*?\.scanner-page__input\s*\{([^}]*)\}",
        text,
        re.S,
    )
    assert rule is not None, "правило узкого экрана для поля сканера исчезло"
    assert "flex" in rule.group(1), (
        "одной ширины мало: во flex-строке поле снова сожмут соседи"
    )
