"""Отмена проведённых продаж и ремонтов: то, что дороже всего ошибиться.

Пришедшие с веткой проверки берут только серийный экземпляр. На этом складе
почти весь товар лежит навалом в лотах, а по документу мог уже пройти
частичный возврат. Здесь проверяется именно это: возвращается только то, что
ещё не вернули, возвращается в свой лот и свою ячейку, второй раз ничего не
возвращается, а цены и себестоимость документа остаются как были.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.catalog.models import Category, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import PartItem, StockLot, StockMovement
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
    RepairError,
    add_stock_lot_to_repair_order,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.models import Sale
from apps.sales.services import (
    SaleError,
    add_stock_lot_to_sale,
    cancel_sale,
    complete_sale,
    create_sale,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="hozyain", password=PASSWORD)


def _line(supplier, part, admin, *, quantity, unit_cost):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def env(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Отмена")
    unit = Unit.objects.get(name="Штука")
    first_cell = StorageLocation.objects.create(
        name="Ячейка 1", code="C-01", storage_allowed=True, is_active=True
    )
    second_cell = StorageLocation.objects.create(
        name="Ячейка 2", code="C-02", storage_allowed=True, is_active=True
    )
    bulk = PartType.objects.create(
        name="БОЛТ", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("200"),
    )
    other = PartType.objects.create(
        name="ГАЙКА", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("300"),
    )
    cheap = create_stock_lot(_line(supplier, bulk, admin, quantity="10", unit_cost="100"),
                             first_cell, Decimal("10"))
    receive_stock_lot(cheap, by=admin)
    dear = create_stock_lot(_line(supplier, bulk, admin, quantity="10", unit_cost="250"),
                            second_cell, Decimal("10"))
    receive_stock_lot(dear, by=admin)
    nuts = create_stock_lot(_line(supplier, other, admin, quantity="8", unit_cost="70"),
                            first_cell, Decimal("8"))
    receive_stock_lot(nuts, by=admin)
    return {
        "admin": admin, "supplier": supplier, "category": category, "unit": unit,
        "first_cell": first_cell, "second_cell": second_cell,
        "bulk": bulk, "other": other, "cheap": cheap, "dear": dear, "nuts": nuts,
    }


def _sale(env, *, lots_and_quantities, price="500", customer=None):
    sale = create_sale(customer_name="Иванов", customer_phone="+79990000001",
                       by=env["admin"], customer=customer)
    for lot, quantity in lots_and_quantities:
        add_stock_lot_to_sale(sale, lot, Decimal(quantity),
                              unit_price=Decimal(price), by=env["admin"])
    return complete_sale(sale, by=env["admin"])


def _return_movements(document_pk, kinds):
    return StockMovement.objects.filter(document_id=document_pk, movement_type__in=kinds)


# --- Продажа: лоты, а не только серийные экземпляры ------------------------------


def test_cancelling_a_lot_sale_returns_stock_to_its_own_lot_and_cell(env):
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "4")])
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("6")
    prices_before = [line.unit_price for line in sale.lines.all()]
    costs_before = [line.unit_cost_rub for line in sale.lines.all()]

    cancel_sale(sale, by=env["admin"], reason="Ошибка оператора", author="Иванов И.")

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")  # вернулось в свой лот
    assert env["cheap"].location_id == env["first_cell"].pk  # и в свою ячейку
    assert env["cheap"].status == StockLot.Status.AVAILABLE
    sale.refresh_from_db()
    assert sale.status == Sale.Status.CANCELED
    assert sale.cancellation_reason == "Ошибка оператора"
    assert sale.cancellation_author == "Иванов И."
    assert sale.canceled_by_id == env["admin"].pk
    assert [line.unit_price for line in sale.lines.all()] == prices_before
    assert [line.unit_cost_rub for line in sale.lines.all()] == costs_before


def test_cancelling_a_multi_lot_sale_returns_each_lot_separately(env):
    """Дешёвый и дорогой лот в разных ячейках: каждый возвращается в свою."""
    sale = _sale(env, lots_and_quantities=[
        (env["cheap"], "3"), (env["dear"], "2"), (env["nuts"], "5"),
    ])

    cancel_sale(sale, by=env["admin"], reason="Клиент отказался", author="Иванов И.")

    for lot, quantity, cell in (
        (env["cheap"], Decimal("10"), env["first_cell"]),
        (env["dear"], Decimal("10"), env["second_cell"]),
        (env["nuts"], Decimal("8"), env["first_cell"]),
    ):
        lot.refresh_from_db()
        assert lot.quantity == quantity
        assert lot.location_id == cell.pk
    assert _return_movements(sale.pk, [StockMovement.MovementType.RETURN_LOT]).count() == 3


def test_an_earlier_partial_return_is_not_returned_twice(env):
    """Продали 5, вернули 2, отменяем: доехать должны ровно оставшиеся 3."""
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "5")])
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("5")

    sale_line = sale.lines.get()
    partial = create_return(source=sale, reason="Часть не подошла", by=env["admin"])
    add_sale_line_return(
        partial, sale_line, Decimal("2"),
        to_location=env["first_cell"], restock_status=StockLot.Status.AVAILABLE,
        by=env["admin"],
    )
    complete_return(partial, by=env["admin"])
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("7")

    cancel_sale(sale, by=env["admin"], reason="Отмена остатка", author="Иванов И.")

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")  # 7 + 3, а не 7 + 5
    # Возврат из продажи помечает свои движения тем же документом, поэтому
    # компенсацию считаем по комментарию отмены, а не по всем движениям.
    compensating = _return_movements(
        sale.pk, [StockMovement.MovementType.RETURN_LOT]
    ).filter(comment__startswith="Отмена продажи")
    assert sum(movement.quantity for movement in compensating) == Decimal("3")


def test_a_fully_returned_sale_creates_no_compensation(env):
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "4")])
    sale_line = sale.lines.get()
    full = create_return(source=sale, reason="Вернули всё", by=env["admin"])
    add_sale_line_return(
        full, sale_line, Decimal("4"),
        to_location=env["first_cell"], restock_status=StockLot.Status.AVAILABLE,
        by=env["admin"],
    )
    complete_return(full, by=env["admin"])
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")
    movements_before = StockMovement.objects.count()

    cancel_sale(sale, by=env["admin"], reason="Закрываем документ", author="Иванов И.")

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")  # ничего сверх не вернулось
    assert StockMovement.objects.count() == movements_before
    sale.refresh_from_db()
    assert sale.status == Sale.Status.CANCELED


def test_cancelling_twice_returns_the_stock_once(env):
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "4")])
    cancel_sale(sale, by=env["admin"], reason="Первая", author="Иванов И.")
    env["cheap"].refresh_from_db()
    after_first = env["cheap"].quantity
    movements = StockMovement.objects.count()

    cancel_sale(sale, by=env["admin"], reason="Вторая", author="Петров П.")

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == after_first == Decimal("10")
    assert StockMovement.objects.count() == movements
    sale.refresh_from_db()
    assert sale.cancellation_reason == "Первая"  # первая отмена не переписана


def test_cancellation_demands_a_reason_and_an_author(env):
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "2")])
    for reason, author in (("", "Иванов И."), ("Причина", ""), ("  ", "  ")):
        with pytest.raises(SaleError):
            cancel_sale(sale, by=env["admin"], reason=reason, author=author)
    sale.refresh_from_db()
    assert sale.status == Sale.Status.COMPLETED
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("8")


def test_only_a_completed_sale_can_be_cancelled(env):
    draft = create_sale(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_sale(draft, env["cheap"], Decimal("2"),
                          unit_price=Decimal("500"), by=env["admin"])
    with pytest.raises(SaleError):
        cancel_sale(draft, by=env["admin"], reason="Причина", author="Иванов И.")


def test_an_open_return_draft_blocks_cancellation(env):
    """Иначе черновик возврата и отмена посчитали бы одно и то же дважды."""
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "4")])
    create_return(source=sale, reason="Разбираемся", by=env["admin"])

    with pytest.raises(SaleError):
        cancel_sale(sale, by=env["admin"], reason="Отмена", author="Иванов И.")

    sale.refresh_from_db()
    assert sale.status == Sale.Status.COMPLETED


def test_cancellation_keeps_the_customer_link_and_snapshot(env):
    customer = Customer.objects.create(name="Саликов Рим Васильевич", phone="+79990000009")
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "2")], customer=customer)
    name_before, phone_before = sale.customer_name, sale.customer_phone

    cancel_sale(sale, by=env["admin"], reason="Отмена", author="Иванов И.")

    sale.refresh_from_db()
    assert sale.customer_id == customer.pk
    assert (sale.customer_name, sale.customer_phone) == (name_before, phone_before)


# --- Ремонт ----------------------------------------------------------------------


def _repair(env, *, lots_and_quantities, customer=None):
    order = create_repair_order(customer_name="Иванов", customer_phone="+79990000001",
                                by=env["admin"], customer=customer)
    for lot, quantity in lots_and_quantities:
        add_stock_lot_to_repair_order(order, lot, Decimal(quantity), by=env["admin"])
    return complete_repair_order(order, by=env["admin"])


def test_cancelling_a_repair_returns_issued_lots_to_their_cells(env):
    order = _repair(env, lots_and_quantities=[(env["cheap"], "3"), (env["dear"], "2")])
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("7")
    costs_before = [line.unit_cost_rub for line in order.lines.all()]
    prices_before = [line.customer_unit_price_rub for line in order.lines.all()]

    cancel_repair_order(order, by=env["admin"], reason="Ремонт отменён", author="Иванов И.")

    env["cheap"].refresh_from_db()
    env["dear"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")
    assert env["dear"].quantity == Decimal("10")
    assert env["cheap"].location_id == env["first_cell"].pk
    assert env["dear"].location_id == env["second_cell"].pk
    order.refresh_from_db()
    assert order.status == RepairOrder.Status.CANCELED
    assert order.cancellation_reason == "Ремонт отменён"
    assert order.cancellation_author == "Иванов И."
    assert order.canceled_by_id == env["admin"].pk
    # Себестоимость склада и цена клиента - разные величины, обе на месте.
    assert [line.unit_cost_rub for line in order.lines.all()] == costs_before
    assert [line.customer_unit_price_rub for line in order.lines.all()] == prices_before


def test_cancelling_a_repair_without_parts_touches_no_stock(env):
    order = create_repair_order(customer_name="Иванов", by=env["admin"])
    order = complete_repair_order(order, by=env["admin"]) if order.lines.exists() else order
    movements = StockMovement.objects.count()

    cancel_repair_order(order, by=env["admin"], reason="Передумали", author="Иванов И.")

    order.refresh_from_db()
    assert order.status == RepairOrder.Status.CANCELED
    assert StockMovement.objects.count() == movements


def test_a_repair_draft_still_cancels_without_a_reason(env):
    """Старый путь отмены черновика не сломан: склада он не касался."""
    order = create_repair_order(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_repair_order(order, env["cheap"], Decimal("2"), by=env["admin"])

    cancel_repair_order(order, by=env["admin"])

    order.refresh_from_db()
    assert order.status == RepairOrder.Status.CANCELED


def test_a_completed_repair_demands_a_reason_and_an_author(env):
    order = _repair(env, lots_and_quantities=[(env["cheap"], "2")])
    for reason, author in (("", "Иванов И."), ("Причина", "")):
        with pytest.raises(RepairError):
            cancel_repair_order(order, by=env["admin"], reason=reason, author=author)
    order.refresh_from_db()
    assert order.status == RepairOrder.Status.COMPLETED
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("8")


def test_cancelling_a_repair_twice_returns_the_stock_once(env):
    order = _repair(env, lots_and_quantities=[(env["cheap"], "3")])
    cancel_repair_order(order, by=env["admin"], reason="Первая", author="Иванов И.")
    env["cheap"].refresh_from_db()
    after_first = env["cheap"].quantity
    movements = StockMovement.objects.count()

    cancel_repair_order(order, by=env["admin"], reason="Вторая", author="Петров П.")

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == after_first == Decimal("10")
    assert StockMovement.objects.count() == movements


# --- Экраны и права ---------------------------------------------------------------


def test_the_sale_card_offers_cancellation_behind_a_confirmation(client, env):
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "2")])
    client.force_login(env["admin"])

    card = client.get(reverse("sale_detail", args=[sale.pk])).content.decode()
    assert "Отменить" in card

    confirm = client.get(reverse("sale_cancel_confirm", args=[sale.pk]))
    assert confirm.status_code == 200
    body = confirm.content.decode()
    assert 'name="reason"' in body
    assert 'name="author"' in body
    sale.refresh_from_db()
    assert sale.status == Sale.Status.COMPLETED  # открытие экрана ничего не меняет


def test_the_repair_card_offers_cancellation_behind_a_confirmation(client, env):
    order = _repair(env, lots_and_quantities=[(env["cheap"], "2")])
    client.force_login(env["admin"])

    card = client.get(reverse("repair_order_detail", args=[order.pk])).content.decode()
    assert "Отменить" in card

    confirm = client.get(reverse("repair_order_cancel_confirm", args=[order.pk]))
    assert confirm.status_code == 200
    body = confirm.content.decode()
    assert 'name="reason"' in body
    assert 'name="author"' in body


def test_cancelling_through_the_screen_records_who_and_why(client, env):
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "2")])
    client.force_login(env["admin"])

    client.post(
        reverse("sale_cancel", args=[sale.pk]),
        {"reason": "Клиент вернул товар", "author": "Иванов И."},
        follow=True,
    )

    sale.refresh_from_db()
    assert sale.status == Sale.Status.CANCELED
    assert sale.cancellation_reason == "Клиент вернул товар"
    assert sale.cancellation_author == "Иванов И."
    assert sale.canceled_by_id == env["admin"].pk  # аудит: кто нажал
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")


def test_a_seller_cannot_cancel_a_completed_sale(client, env, django_user_model):
    """Возврат на склад продавцу не выдан именно затем, чтобы продажу нельзя
    было отменить тихо. Отмена не должна становиться обходным путём."""
    from apps.accounts import roles

    sale = _sale(env, lots_and_quantities=[(env["cheap"], "2")])
    seller = django_user_model.objects.create_user(username="prodavec", password=PASSWORD)
    seller.groups.add(Group.objects.get(name=roles.SELLER))
    assert seller.can_manage_sales and not seller.can_manage_returns
    client.login(username="prodavec", password=PASSWORD)

    denied = client.post(
        reverse("sale_cancel", args=[sale.pk]),
        {"reason": "Хочу", "author": "Продавец"},
    )
    screen = client.get(reverse("sale_cancel_confirm", args=[sale.pk]))

    assert denied.status_code in (403, 302)
    assert screen.status_code in (403, 302)
    sale.refresh_from_db()
    assert sale.status == Sale.Status.COMPLETED
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("8")


def test_a_seller_cannot_cancel_a_completed_repair(client, env, django_user_model):
    from apps.accounts import roles

    order = _repair(env, lots_and_quantities=[(env["cheap"], "2")])
    seller = django_user_model.objects.create_user(username="master", password=PASSWORD)
    seller.groups.add(Group.objects.get(name=roles.SELLER))
    client.login(username="master", password=PASSWORD)

    denied = client.post(
        reverse("repair_order_cancel", args=[order.pk]),
        {"reason": "Хочу", "author": "Мастер"},
    )

    assert denied.status_code in (403, 302)
    order.refresh_from_db()
    assert order.status == RepairOrder.Status.COMPLETED
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("8")


def test_a_storekeeper_may_cancel_a_completed_repair(client, env, django_user_model):
    """У кладовщика возврат есть, значит и отмена выдачи ему разрешена."""
    from apps.accounts import roles

    order = _repair(env, lots_and_quantities=[(env["cheap"], "2")])
    keeper = django_user_model.objects.create_user(username="kladovshik", password=PASSWORD)
    keeper.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    client.login(username="kladovshik", password=PASSWORD)

    client.post(
        reverse("repair_order_cancel", args=[order.pk]),
        {"reason": "Ремонт не состоялся", "author": "Кладовщик"}, follow=True,
    )

    order.refresh_from_db()
    assert order.status == RepairOrder.Status.CANCELED
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")


# --- Серийный экземпляр возвращается в свою ячейку ---------------------------------


def test_a_serial_item_goes_back_to_the_cell_it_left(env):
    serial = PartType.objects.create(
        name="НАСОС", category=env["category"], unit=env["unit"],
        tracking_mode=PartType.TrackingMode.SERIAL, recommended_price=Decimal("900"),
    )
    line = _line(env["supplier"], serial, env["admin"], quantity="1", unit_cost="400")
    item = create_part_items(line, 1, serial_number="SN-1")[0]
    receive_part_item(item, to_location=env["second_cell"], by=env["admin"])
    sale = create_sale(customer_name="Иванов", by=env["admin"])
    from apps.sales.services import add_part_item_to_sale

    add_part_item_to_sale(sale, item, unit_price=Decimal("1200"), by=env["admin"])
    complete_sale(sale, by=env["admin"])
    item.refresh_from_db()
    assert item.status == PartItem.Status.SOLD

    cancel_sale(sale, by=env["admin"], reason="Возврат", author="Иванов И.")

    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert item.current_location_id == env["second_cell"].pk  # своя ячейка, не чужая


# --- Отчёты и статус оплаты --------------------------------------------------------


def _acknowledge(customer, admin):
    from apps.customers.services import acknowledge_customer_period_payment
    from apps.reports.services import resolve_period

    period = resolve_period({})
    return acknowledge_customer_period_payment(
        customer_id=customer.pk, period=period, by=admin
    ), period


def _paid(customer, period):
    from apps.reports.payment_status import payment_statuses_for_rows

    rows = [{"customer_id": customer.pk, "linked": True}]
    return payment_statuses_for_rows(rows=rows, period=period)[customer.pk]["paid"]


def test_cancelling_a_sale_makes_a_paid_period_stale(env):
    """Отмена меняет состав начислений, значит подтверждение оплаты устарело."""
    customer = Customer.objects.create(name="Саликов Рим Васильевич", phone="+79990000009")
    sale = _sale(env, lots_and_quantities=[(env["cheap"], "3")], customer=customer)
    acknowledgement, period = _acknowledge(customer, env["admin"])
    assert _paid(customer, period) is True

    cancel_sale(sale, by=env["admin"], reason="Клиент вернул", author="Иванов И.")

    assert _paid(customer, period) is False  # сумма периода изменилась
    acknowledgement.refresh_from_db()
    assert acknowledgement.revoked_at is None  # подтверждение не стёрто, оно устарело


def test_cancelling_a_repair_makes_a_paid_period_stale(env):
    customer = Customer.objects.create(name="Петров Иван", phone="+79990000010")
    order = _repair(env, lots_and_quantities=[(env["cheap"], "2")], customer=customer)
    _, period = _acknowledge(customer, env["admin"])
    assert _paid(customer, period) is True

    cancel_repair_order(order, by=env["admin"], reason="Отменён", author="Иванов И.")

    assert _paid(customer, period) is False


def test_every_client_report_leads_to_the_document_card():
    """Оператор не должен искать отмену: из строки отчёта есть путь в карточку.

    Кнопка отмены живёт на карточке документа, а не в каждой ячейке таблицы,
    поэтому проверяем именно наличие ссылки на карточку в тех отчётах, которые
    назвал пользователь.
    """
    root = Path(__file__).resolve().parent.parent / "templates"
    for name, route in (
        ("actions/report.html", "sale_detail"),
        ("actions/report.html", "repair_order_detail"),
        ("reports/sales_by_client_operations.html", "sale_detail"),
        ("reports/repairs_by_client_operations.html", "repair_order_detail"),
    ):
        markup = (root / name).read_text(encoding="utf-8")
        assert f"url '{route}'" in markup, f"{name} не ведёт в {route}"
