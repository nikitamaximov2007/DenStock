"""Стоимость деталей, выданных в ремонт, в отчётах по клиенту.

Для продаж сумма показывалась, для ремонтов не показывалось ничего, и
сотрудник не мог узнать, во сколько складу обошлась выдача.

Главное правило, которое здесь закреплено: у ремонта нет выручки. Система
хранит себестоимость выданных деталей, замороженную при проведении заказа, и
складывать её с суммой продажи нельзя. Поэтому величины живут в разных
колонках, и у каждой строки заполнена ровно одна из них.

Второе правило: для прошлого ремонта нельзя брать сегодняшнюю цену из
каталога. Источник только исторический.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    add_part_item_to_repair_order,
    add_stock_lot_to_repair_order,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import get_client_part_history, resolve_period
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
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
    return make_user("boss", is_superuser=True)


def _lot(part, location, qty, supplier, admin, *, unit_cost):
    """Партия со своей закупочной ценой: так задаётся историческая себестоимость."""
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(str(qty)), unit_cost_currency=Decimal(str(unit_cost)),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def data(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S07-D01-C01", storage_allowed=True, is_active=True
    )
    parts, lots = {}, {}
    # Закупочные цены намеренно разные: так видно, что берётся своя у каждой.
    for key, name, number, cost in (
        ("belt", "Ремень", "800100", "300"),
        # 155 намеренно: при количестве 2 итог не совпадёт с ремнём за 300.
        ("filter", "Фильтр", "800200", "155"),
        ("plug", "Свеча", "800300", "90"),
    ):
        part = PartType.objects.create(
            name=name, category=category, unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal("999"),
        )
        PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM)
        parts[key] = part
        lots[key] = _lot(part, location, 100, supplier, admin, unit_cost=cost)
    return {"admin": admin, "parts": parts, "lots": lots, "loc": location, "sup": supplier}


def _repair(data, *, customer, items):
    order = create_repair_order(customer=customer, customer_name="", by=data["admin"])
    for key, qty in items:
        add_stock_lot_to_repair_order(order, data["lots"][key], Decimal(str(qty)), by=data["admin"])
    return complete_repair_order(order, by=data["admin"])


def _sale(data, *, customer, items, price="500"):
    sale = create_sale(customer=customer, customer_name="", by=data["admin"])
    for key, qty in items:
        add_stock_lot_to_sale(
            sale, data["lots"][key], Decimal(str(qty)), unit_price=Decimal(price), by=data["admin"]
        )
    return complete_sale(sale, by=data["admin"])


def _login(client, user):
    client.login(username=user.username, password=PASSWORD)


# --- Источник величины -------------------------------------------------------------


def test_the_repair_cost_comes_from_the_frozen_line(data):
    """Себестоимость замораживается при проведении и живёт на строке заказа."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1),))

    line = order.lines.get()
    assert line.unit_cost_rub > 0, "себестоимость единицы не заморожена"
    assert line.total_cost_rub == line.unit_cost_rub * line.quantity

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    repair_rows = [row for row in rows if row["kind"] == "repair"]
    assert len(repair_rows) == 1
    assert repair_rows[0]["cost"] == line.total_cost_rub


def test_a_later_catalog_price_change_does_not_move_a_past_repair(data):
    """Для прошлого ремонта сегодняшняя цена каталога значения не имеет."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 2),))
    before = order.lines.get().total_cost_rub

    part = data["parts"]["belt"]
    part.recommended_price = Decimal("99999")
    part.save(update_fields=["recommended_price"])

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["cost"] == before
    assert rows[0]["cost"] != Decimal("99999")


def test_quantity_is_already_included_in_the_line_total(data):
    """Показывается итог строки, а не цена единицы: делить ничего не нужно."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 3),))
    line = order.lines.get()

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["cost"] == line.unit_cost_rub * Decimal("3")


def test_each_part_of_a_repair_keeps_its_own_cost(data):
    """У деталей разная закупочная цена, и смешивать их нельзя."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1), ("filter", 2), ("plug", 1)))

    by_part = {line.part_type.name: line.total_cost_rub for line in order.lines.all()}
    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    from_report = {row["part_name"]: row["cost"] for row in rows if row["kind"] == "repair"}
    assert from_report == by_part
    assert len(set(by_part.values())) == 3, "проверка бессмысленна: себестоимости совпали"


# --- Разделение выручки и себестоимости ------------------------------------------------


def test_sale_and_repair_keep_customer_amount_separate_from_cost(data):
    """Repair client price is a snapshot, not a relabelled procurement cost."""
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("belt", 1),))
    _repair(data, customer=customer, items=(("filter", 1),))

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    for row in rows:
        if row["kind"] == "sale":
            assert row["amount"] is not None
            assert row["cost"] is None, "у продажи появилась себестоимость"
        else:
            assert row["cost"] is not None
            assert row["amount"] is not None
            assert row["amount"] != row["cost"]


def test_the_sale_amount_is_unchanged(data):
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("belt", 2),), price="500")

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    sale_rows = [row for row in rows if row["kind"] == "sale"]
    assert sale_rows[0]["amount"] == sale.lines.get().total_price == Decimal("1000")


# --- Оба экрана ------------------------------------------------------------------------


def test_the_combined_report_shows_the_two_values_in_separate_columns(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("belt", 1),))
    _repair(data, customer=customer, items=(("filter", 1),))

    body = client.get(
        reverse("reports_client_timeline"), {"customer_id": customer.pk}
    ).content.decode()
    assert "Сумма (₽)" in body
    assert "Себестоимость (₽)" in body
    assert "стоимость детали для клиента" in body


def test_the_repairs_report_shows_the_cost(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 2),))
    expected = order.lines.get().total_cost_rub

    body = client.get(
        reverse("reports_repairs_by_client_detail"), {"customer_id": customer.pk}
    ).content.decode()
    assert "Себестоимость (₽)" in body
    assert "выручка" not in body.lower() or "не выручка" in body.lower()
    digits = f"{int(expected)}"
    assert digits in body.replace("&nbsp;", "").replace(" ", ""), "себестоимость не показана"


def test_the_repairs_report_never_calls_it_revenue(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer, items=(("belt", 1),))

    body = client.get(
        reverse("reports_repairs_by_client_detail"), {"customer_id": customer.pk}
    ).content.decode()
    assert "Выручка" not in body, "себестоимость названа выручкой"


# --- Права и период ----------------------------------------------------------------------


def test_a_role_without_cost_rights_sees_no_cost(client, data, make_user):
    """Себестоимость закрыта тем же правом, что и остальные закупочные суммы."""
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer, items=(("belt", 1),))
    keeper = make_user("kladovshik", role=roles.STOREKEEPER)
    _login(client, keeper)

    response = client.get(
        reverse("reports_repairs_by_client_detail"), {"customer_id": customer.pk}
    )
    assert response.status_code == 200
    assert response.context["show_costs"] is False
    assert "Себестоимость (₽)" not in response.content.decode()


def test_a_repair_outside_the_period_is_not_shown(data):
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1),))
    RepairOrder.objects.filter(pk=order.pk).update(
        completed_at=timezone.now() - timezone.timedelta(days=400)
    )

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert not [row for row in rows if row["kind"] == "repair"]


def test_two_repairs_are_not_counted_twice(data):
    customer = Customer.objects.create(name="Иванов")
    first = _repair(data, customer=customer, items=(("belt", 1),))
    second = _repair(data, customer=customer, items=(("belt", 1),))

    rows = [
        row for row in get_client_part_history(resolve_period({}), customer_id=customer.pk)
        if row["kind"] == "repair"
    ]
    assert len(rows) == 2
    total = sum(row["cost"] for row in rows)
    expected = first.lines.get().total_cost_rub + second.lines.get().total_cost_rub
    assert total == expected


def test_a_new_delivery_at_another_price_does_not_move_a_past_repair(data, admin):
    """Самая сильная форма проверки историчности.

    После проведённого ремонта меняется всё, что могло бы на него повлиять:
    цена в каталоге и закупочная цена новой партии той же детали. Прошлая
    выдача обязана остаться прежней - иначе отчёт за закрытый месяц начинал бы
    меняться сам по себе.
    """
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 2),))
    frozen = order.lines.get().total_cost_rub
    assert frozen > 0

    part = data["parts"]["belt"]
    part.recommended_price = Decimal("99999")
    part.min_price = Decimal("88888")
    part.save(update_fields=["recommended_price", "min_price"])
    _lot(part, data["loc"], 50, data["sup"], admin, unit_cost="12345")

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["cost"] == frozen
    order.lines.get().refresh_from_db()
    assert order.lines.get().total_cost_rub == frozen


def test_a_new_delivery_does_not_move_a_past_sale(data, admin):
    """То же для продажи: её сумма - снимок проведённого документа."""
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("belt", 2),), price="500")
    before = sale.lines.get().total_price

    part = data["parts"]["belt"]
    part.recommended_price = Decimal("99999")
    part.save(update_fields=["recommended_price"])
    _lot(part, data["loc"], 50, data["sup"], admin, unit_cost="12345")

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["amount"] == before == Decimal("1000")


# --- Одна и та же деталь внутри одного ремонта -------------------------------------------


def test_two_lots_of_one_part_keep_their_own_costs(data, admin):
    """Строка выдачи всегда относится к одному лоту, а не к нескольким сразу.

    Поэтому себестоимость единицы - настоящая историческая величина, а итог
    строки получается умножением, а не усреднением. Если бы строка собирала
    несколько лотов, показывать «цену за штуку» было бы нельзя.
    """
    customer = Customer.objects.create(name="Иванов")
    part = data["parts"]["belt"]
    second_lot = _lot(part, data["loc"], 100, data["sup"], admin, unit_cost="500")

    order = create_repair_order(customer=customer, customer_name="", by=admin)
    add_stock_lot_to_repair_order(order, data["lots"]["belt"], Decimal("1"), by=admin)
    add_stock_lot_to_repair_order(order, second_lot, Decimal("1"), by=admin)
    complete_repair_order(order, by=admin)

    costs = sorted(line.total_cost_rub for line in order.lines.all())
    assert len(costs) == 2, "выдачи из разных лотов слиплись в одну строку"
    assert costs[0] != costs[1], "проверка бессмысленна: себестоимости совпали"

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert sorted(row["cost"] for row in rows) == costs


def test_every_line_total_is_exactly_its_unit_cost_times_quantity(data):
    """Количество учтено ровно один раз: второй множитель нигде не появляется."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 4), ("filter", 3)))

    for line in order.lines.all():
        assert line.total_cost_rub == line.unit_cost_rub * line.quantity

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    for row in rows:
        line = order.lines.get(part_type__name=row["part_name"])
        assert row["cost"] == line.unit_cost_rub * row["quantity"]


def test_a_serial_item_carries_its_own_frozen_cost(db, admin):
    """У экземпляра себестоимость своя, и берётся она с экземпляра, а не с лота."""
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка S", code="S09-D01-C01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Насос", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
    )
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal("1"), unit_cost_currency=Decimal("777"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    item = create_part_items(line, 1, serial_number="SN-OTCHET-1")[0]
    receive_part_item(item, to_location=location, by=admin)

    customer = Customer.objects.create(name="Иванов")
    order = create_repair_order(customer=customer, customer_name="", by=admin)
    add_part_item_to_repair_order(order, item, by=admin)
    complete_repair_order(order, by=admin)

    issued = order.lines.get()
    assert issued.part_item_id == item.pk
    assert issued.unit_cost_rub == item.landed_cost_rub
    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["cost"] == issued.total_cost_rub


# --- Что в отчёт не попадает вовсе --------------------------------------------------------


def test_a_draft_repair_is_not_in_the_report(data):
    """Незавершённый заказ склада ещё не касался, и себестоимости у него нет."""
    customer = Customer.objects.create(name="Иванов")
    order = create_repair_order(customer=customer, customer_name="", by=data["admin"])
    add_stock_lot_to_repair_order(order, data["lots"]["belt"], Decimal("1"), by=data["admin"])

    assert order.lines.get().total_cost_rub == 0, "себестоимость заморожена раньше времени"
    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert not rows


def test_a_canceled_repair_is_not_in_the_report(data):
    customer = Customer.objects.create(name="Иванов")
    order = create_repair_order(customer=customer, customer_name="", by=data["admin"])
    add_stock_lot_to_repair_order(order, data["lots"]["belt"], Decimal("1"), by=data["admin"])
    cancel_repair_order(order, by=data["admin"])

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert not rows


def test_a_completed_repair_cannot_be_canceled_afterwards(data):
    """Отчёт исторический, потому что проведённый заказ неизменяем."""
    from apps.repairs.services import RepairError

    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1),))
    with pytest.raises(RepairError):
        cancel_repair_order(order, by=data["admin"])


# --- Границы периода ----------------------------------------------------------------------


def _move_completion(order, when):
    RepairOrder.objects.filter(pk=order.pk).update(completed_at=when)


def test_a_repair_in_the_very_last_second_of_the_day_is_included(data):
    """Граница периода берётся до конца суток, а не до полуночи."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1),))
    today = timezone.localdate()
    late = timezone.make_aware(
        timezone.datetime.combine(today, timezone.datetime.max.time())
    )
    _move_completion(order, late)

    period = resolve_period({"date_from": today.isoformat(), "date_to": today.isoformat()})
    rows = get_client_part_history(period, customer_id=customer.pk)
    assert len(rows) == 1


def test_a_repair_just_before_midnight_of_the_previous_day_is_excluded(data):
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 1),))
    today = timezone.localdate()
    yesterday_late = timezone.make_aware(
        timezone.datetime.combine(today - timedelta(days=1), timezone.datetime.max.time())
    )
    _move_completion(order, yesterday_late)

    period = resolve_period({"date_from": today.isoformat(), "date_to": today.isoformat()})
    assert not get_client_part_history(period, customer_id=customer.pk)


# --- Числа --------------------------------------------------------------------------------


def test_a_fractional_quantity_is_rounded_to_kopecks(data):
    """Дробное количество не должно давать хвост из лишних знаков."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("filter", "0.333"),))
    line = order.lines.get()

    assert line.total_cost_rub == line.total_cost_rub.quantize(Decimal("0.01"))
    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["cost"].as_tuple().exponent == -2


def test_a_part_that_cost_the_warehouse_nothing_shows_a_real_zero(data, admin):
    """Ноль здесь означает «досталось бесплатно», а не «неизвестно».

    Себестоимость замораживается вместе с временем выдачи, поэтому строки без
    известной стоимости в отчёт попасть не могут: незавершённый заказ туда не
    входит вовсе.
    """
    customer = Customer.objects.create(name="Иванов")
    free = PartType.objects.create(
        name="Заглушка", category=Category.objects.first(),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
    )
    lot = _lot(free, data["loc"], 10, data["sup"], admin, unit_cost="0")
    order = create_repair_order(customer=customer, customer_name="", by=admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("2"), by=admin)
    complete_repair_order(order, by=admin)

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert rows[0]["cost"] == Decimal("0.00")
    assert rows[0]["cost"] is not None, "настоящий ноль превратили в пустоту"


def test_a_sale_and_repair_keep_customer_amount_and_cost_distinct(data):
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("belt", 1),), price="500")
    _repair(data, customer=customer, items=(("belt", 1),))

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert len(rows) == 2
    amounts = [row["amount"] for row in rows if row["amount"] is not None]
    costs = [row["cost"] for row in rows if row["cost"] is not None]
    assert len(amounts) == 2 and len(costs) == 1
    sale = next(row for row in rows if row["kind"] == "sale")
    repair = next(row for row in rows if row["kind"] == "repair")
    assert sale["cost"] is None
    assert repair["amount"] != repair["cost"]


# --- Запросы к базе -------------------------------------------------------------------------


def test_the_combined_report_does_not_query_once_per_line(client, data, admin):
    """Вторая денежная колонка не должна была добавить запрос на строку."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    for _ in range(3):
        _sale(data, customer=customer, items=(("belt", 1), ("filter", 1)))
        _repair(data, customer=customer, items=(("belt", 1), ("plug", 1)))

    url = reverse("reports_client_timeline")
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url, {"customer_id": customer.pk})
    assert response.status_code == 200
    few = len(captured)

    for _ in range(6):
        _sale(data, customer=customer, items=(("belt", 1), ("filter", 1)))
        _repair(data, customer=customer, items=(("belt", 1), ("plug", 1)))
    with CaptureQueriesContext(connection) as captured:
        client.get(url, {"customer_id": customer.pk})
    many = len(captured)

    assert many <= few + 2, f"запросы растут вместе со строками: было {few}, стало {many}"


def test_the_repairs_detail_does_not_query_once_per_line(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    for _ in range(3):
        _repair(data, customer=customer, items=(("belt", 1), ("filter", 1), ("plug", 1)))

    url = reverse("reports_repairs_by_client_detail")
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url, {"customer_id": customer.pk})
    assert response.status_code == 200
    few = len(captured)

    for _ in range(6):
        _repair(data, customer=customer, items=(("belt", 1), ("filter", 1), ("plug", 1)))
    with CaptureQueriesContext(connection) as captured:
        client.get(url, {"customer_id": customer.pk})
    many = len(captured)

    assert many <= few + 2, f"запросы растут вместе со строками: было {few}, стало {many}"


# --- Себестоимость никуда не утекает --------------------------------------------------------


def test_these_two_screens_have_no_download_that_could_bypass_the_permission(client, data, admin):
    """Выгрузки у этих двух экранов намеренно нет: они только на экране.

    Выгрузки есть у сводных отчётов за период, и там себестоимость закрыта тем
    же правом. Если выгрузку когда-нибудь добавят сюда, эта проверка напомнит
    закрыть и её.
    """
    from django.urls import get_resolver

    names = set()
    for pattern in get_resolver().url_patterns:
        for sub in getattr(pattern, "url_patterns", []):
            if getattr(sub, "name", None):
                names.add(sub.name)
    leaking = {
        name for name in names
        if "export" in name and ("client" in name or "timeline" in name)
    }
    assert not leaking, f"появилась выгрузка без проверки права: {leaking}"


def test_a_role_without_the_right_never_receives_the_cost_in_the_page(client, data, make_user):
    """Проверяется отданная страница, а не только флаг в контексте."""
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 3),))
    exact = f"{order.lines.get().total_cost_rub}"

    keeper = make_user("kladovshik2", role=roles.STOREKEEPER)
    _login(client, keeper)
    for url in ("reports_client_timeline", "reports_repairs_by_client_detail"):
        body = client.get(reverse(url), {"customer_id": customer.pk}).content.decode()
        assert "Себестоимость" not in body, f"{url}: заголовок виден без права"
        assert exact not in body.replace("&nbsp;", "").replace(" ", ""), (
            f"{url}: сама сумма попала в страницу"
        )
