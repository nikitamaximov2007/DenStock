"""Partial cancellation of one issued repair line from the client report."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import PartItem, StockMovement
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
    add_part_item_to_repair_order,
    add_stock_lot_to_repair_order,
    cancel_repair_line_quantity,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
    reversible_quantity,
)
from apps.reports.payment_status import payment_statuses_for_rows
from apps.reports.services import (
    get_client_part_history,
    get_clients_sales_and_repairs,
    resolve_period,
)
from apps.returns.models import StockReturnLine
from apps.returns.services import add_repair_line_return, complete_return, create_return
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="hozyain", password=PASSWORD)


def _batch_line(supplier, part, admin, *, quantity, unit_cost):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def env(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Частичная отмена ремонта")
    unit = Unit.objects.get(name="Штука")
    first = StorageLocation.objects.create(
        name="Ячейка 1", code="RC-01", storage_allowed=True, is_active=True
    )
    second = StorageLocation.objects.create(
        name="Ячейка 2", code="RC-02", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="REPAIR PISTON",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("900"),
    )
    cheap = create_stock_lot(
        _batch_line(supplier, part, admin, quantity="10", unit_cost="600"),
        first,
        Decimal("10"),
    )
    receive_stock_lot(cheap, by=admin)
    dear = create_stock_lot(
        _batch_line(supplier, part, admin, quantity="10", unit_cost="750"),
        second,
        Decimal("10"),
    )
    receive_stock_lot(dear, by=admin)
    return {
        "admin": admin,
        "supplier": supplier,
        "category": category,
        "unit": unit,
        "first": first,
        "second": second,
        "part": part,
        "cheap": cheap,
        "dear": dear,
    }


def _repair(env, *, quantity="4", customer=None, lot=None, customer_price="1000"):
    order = create_repair_order(customer_name="Иванов", customer=customer, by=env["admin"])
    add_stock_lot_to_repair_order(
        order,
        lot or env["cheap"],
        Decimal(quantity),
        customer_unit_price_rub=Decimal(customer_price),
        by=env["admin"],
    )
    return complete_repair_order(order, by=env["admin"])


def _ordinary_return(env, order, line, quantity):
    document = create_return(source=order, reason="Обычный возврат", by=env["admin"])
    add_repair_line_return(
        document,
        line,
        Decimal(quantity),
        to_location=env["first"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=env["admin"],
    )
    return complete_return(document, by=env["admin"])


def test_cancelling_one_of_four_preserves_history_and_recalculates_effective_values(env):
    customer = Customer.objects.create(name="Рим Саликов")
    order = _repair(env, customer=customer)
    line = order.lines.get()
    assert line.customer_unit_price_rub == Decimal("1000")
    assert line.unit_cost_rub == Decimal("600")

    document = cancel_repair_line_quantity(
        line, 1, reason="Передумали", author="Иванов И.", by=env["admin"]
    )

    env["cheap"].refresh_from_db()
    order.refresh_from_db()
    line.refresh_from_db()
    returned = document.lines.get()
    assert env["cheap"].quantity == Decimal("7")
    assert line.quantity == Decimal("4")
    assert line.customer_unit_price_rub == Decimal("1000")
    assert line.unit_cost_rub == Decimal("600")
    assert returned.source_repair_line_id == line.pk
    assert returned.stock_lot_id == env["cheap"].pk
    assert returned.to_location_id == env["first"].pk
    assert returned.unit_cost_rub == Decimal("600")
    assert returned.total_cost_rub == Decimal("600")
    assert reversible_quantity(line) == Decimal("3")
    assert order.status == RepairOrder.Status.COMPLETED
    assert order.cost_total == Decimal("1800")
    assert (
        StockMovement.objects.filter(movement_type=StockMovement.MovementType.RETURN_LOT).count()
        == 1
    )
    period = resolve_period({})
    client_row = next(
        row for row in get_clients_sales_and_repairs(period) if row["customer_id"] == customer.pk
    )
    assert client_row["repair_quantity"] == Decimal("3")
    assert client_row["repair_customer_amount"] == Decimal("3000")
    assert client_row["issued_cost"] == Decimal("1800")
    history = get_client_part_history(period, customer_id=customer.pk)
    assert history[0]["quantity"] == Decimal("3")
    assert history[0]["amount"] == Decimal("3000")


def test_ordinary_returns_partial_cancellations_and_full_cancel_do_not_double_restore(env):
    order = _repair(env, quantity="5")
    line = order.lines.get()
    _ordinary_return(env, order, line, "1")
    cancel_repair_line_quantity(line, 2, reason="Часть", author="И.", by=env["admin"])
    assert reversible_quantity(line) == Decimal("2")

    cancel_repair_order(order, by=env["admin"], reason="Остаток", author="Иванов И.")
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")
    movements = StockMovement.objects.count()
    cancel_repair_order(order, by=env["admin"], reason="Повтор", author="И.")
    assert StockMovement.objects.count() == movements


def test_multiple_partial_cancellations_walk_one_line_down_without_duplicate_movements(env):
    order = _repair(env, quantity="4")
    line = order.lines.get()

    for quantity, reason, remaining in (
        ("1", "Первая", "3"),
        ("2", "Вторая", "1"),
        ("1", "Третья", "0"),
    ):
        cancel_repair_line_quantity(
            line, quantity, reason=reason, author="И.", by=env["admin"]
        )
        assert reversible_quantity(line) == Decimal(remaining)

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")
    assert StockReturnLine.objects.filter(source_repair_line=line).count() == 3
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_LOT
    ).count() == 3
    with pytest.raises(RepairError):
        cancel_repair_line_quantity(line, 1, reason="Повтор", author="И.", by=env["admin"])
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_LOT
    ).count() == 3


def test_reversal_is_exact_for_two_lines_from_different_lots(env):
    order = create_repair_order(customer_name="Иванов", by=env["admin"])
    first_line = add_stock_lot_to_repair_order(
        order, env["cheap"], Decimal("2"), customer_unit_price_rub=Decimal("1000"), by=env["admin"]
    )
    second_line = add_stock_lot_to_repair_order(
        order, env["dear"], Decimal("2"), customer_unit_price_rub=Decimal("1000"), by=env["admin"]
    )
    complete_repair_order(order, by=env["admin"])

    cancel_repair_line_quantity(
        second_line, 1, reason="Только второй", author="И.", by=env["admin"]
    )
    env["cheap"].refresh_from_db()
    env["dear"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("8")
    assert env["dear"].quantity == Decimal("9")
    assert reversible_quantity(first_line) == Decimal("2")
    assert reversible_quantity(second_line) == Decimal("1")


def test_reason_author_and_serial_quantity_are_validated(env):
    order = _repair(env)
    line = order.lines.get()
    for reason, author in (("", "И."), ("Причина", "")):
        with pytest.raises(RepairError):
            cancel_repair_line_quantity(line, 1, reason=reason, author=author, by=env["admin"])

    serial = PartType.objects.create(
        name="REPAIR SERIAL",
        category=env["category"],
        unit=env["unit"],
        tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("1200"),
    )
    item = create_part_items(
        _batch_line(env["supplier"], serial, env["admin"], quantity="1", unit_cost="700"), 1
    )[0]
    receive_part_item(item, to_location=env["second"], by=env["admin"])
    serial_order = create_repair_order(customer_name="Петров", by=env["admin"])
    serial_line = add_part_item_to_repair_order(serial_order, item, by=env["admin"])
    complete_repair_order(serial_order, by=env["admin"])
    with pytest.raises(RepairError):
        cancel_repair_line_quantity(
            serial_line, "0.5", reason="Часть", author="И.", by=env["admin"]
        )
    cancel_repair_line_quantity(serial_line, 1, reason="Целиком", author="И.", by=env["admin"])
    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert item.current_location_id == env["second"].pk


def test_report_button_confirm_screen_and_redirect_keep_filters(client, env):
    customer = Customer.objects.create(name="Рим Саликов")
    order = _repair(env, customer=customer)
    line = order.lines.get()
    client.force_login(env["admin"])
    report_url = (
        f"{reverse('reports_repairs_by_client_detail')}?customer_id={customer.pk}"
        "&date_from=2026-08-01&date_to=2026-08-31"
    )

    body = client.get(report_url).content.decode()
    assert reverse("repair_line_cancel", args=[line.pk]) in body
    assert "Отменить" in body
    confirm = client.get(reverse("repair_line_cancel", args=[line.pk]), {"next": report_url})
    confirm_body = confirm.content.decode()
    assert confirm.status_code == 200
    assert 'name="quantity"' in confirm_body
    assert 'name="reason"' in confirm_body
    assert 'name="author"' in confirm_body
    assert 'max="4"' in confirm_body

    response = client.post(
        reverse("repair_line_cancel", args=[line.pk]),
        {"quantity": "1", "reason": "Ошибка", "author": "И.", "next": report_url},
        follow=True,
    )
    assert response.redirect_chain[-1][0] == report_url
    after = response.content.decode()
    assert ">3<" in after.replace(" ", "").replace("\n", "")


def test_report_hides_action_without_repair_and_return_permissions(client, env, django_user_model):
    customer = Customer.objects.create(name="Рим Саликов")
    order = _repair(env, customer=customer)
    line = order.lines.get()
    viewer = django_user_model.objects.create_user(username="viewer", password=PASSWORD)
    viewer.groups.add(Group.objects.get(name=roles.VIEWER))
    client.force_login(viewer)
    report = client.get(reverse("reports_repairs_by_client_detail"), {"customer_id": customer.pk})
    assert report.status_code == 200
    assert reverse("repair_line_cancel", args=[line.pk]) not in report.content.decode()
    denied = client.post(
        reverse("repair_line_cancel", args=[line.pk]),
        {"quantity": "1", "reason": "Нет", "author": "Наблюдатель"},
    )
    assert denied.status_code == 403


def test_reversal_makes_client_payment_acknowledgement_stale(env):
    from apps.customers.services import acknowledge_customer_period_payment

    customer = Customer.objects.create(name="Рим Саликов")
    order = _repair(env, customer=customer)
    period = resolve_period({})
    acknowledge_customer_period_payment(customer_id=customer.pk, period=period, by=env["admin"])
    rows = get_clients_sales_and_repairs(period)
    assert payment_statuses_for_rows(rows=rows, period=period)[customer.pk]["paid"] is True

    cancel_repair_line_quantity(order.lines.get(), 1, reason="Ошибка", author="И.", by=env["admin"])
    assert payment_statuses_for_rows(rows=rows, period=period)[customer.pk]["paid"] is False


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_postgresql_two_operators_cannot_reverse_same_repair_unit(django_user_model):
    if connection.vendor != "postgresql":
        pytest.skip("Нужен PostgreSQL через DENSTOCK_TEST_DATABASE_URL")
    admin = django_user_model.objects.create_superuser(username="parallel", password=PASSWORD)
    supplier = Supplier.objects.create(name="ООО Поставка")
    location = StorageLocation.objects.create(
        name="Ячейка", code="RPC-01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="REPAIR NUT",
        category=Category.objects.create(name="Параллельная отмена ремонта"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("100"),
    )
    lot = create_stock_lot(
        _batch_line(supplier, part, admin, quantity="2", unit_cost="60"), location, Decimal("2")
    )
    receive_stock_lot(lot, by=admin)
    order = create_repair_order(customer_name="Иванов", by=admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("1"), by=admin)
    complete_repair_order(order, by=admin)
    line = order.lines.get()

    def attempt():
        close_old_connections()
        try:
            cancel_repair_line_quantity(line, 1, reason="Гонка", author="И.", by=admin)
            return "ok"
        except Exception as exc:  # noqa: BLE001 - one concurrent request must lose
            return type(exc).__name__
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=30) for future in (pool.submit(attempt), pool.submit(attempt))
        ]
    lot.refresh_from_db()
    assert outcomes.count("ok") == 1
    assert lot.quantity == Decimal("2")
    assert reversible_quantity(line) == Decimal("0")
    assert (
        StockMovement.objects.filter(movement_type=StockMovement.MovementType.RETURN_LOT).count()
        == 1
    )
