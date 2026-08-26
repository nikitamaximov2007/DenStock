"""Payment acknowledgement in the combined client sales-and-repairs report."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.customers.models import Customer, CustomerPeriodPaymentAcknowledgement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.payment_status import customer_payment_state, payment_statuses_for_rows
from apps.reports.services import get_clients_sales_and_repairs, resolve_period
from apps.returns.models import StockReturnLine
from apps.returns.services import (
    add_repair_line_return,
    add_sale_line_return,
    cancel_return,
    complete_return,
    create_return,
)
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="admin", password=PASSWORD)


@pytest.fixture
def manager(django_user_model):
    user = django_user_model.objects.create_user(username="manager", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.MANAGER))
    return user


@pytest.fixture
def viewer(django_user_model):
    user = django_user_model.objects.create_user(username="viewer", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.VIEWER))
    return user


@pytest.fixture
def stock(admin):
    supplier = Supplier.objects.create(name="Поставка")
    category = Category.objects.create(name="Расходники")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True
    )
    part = PartType.objects.create(
        name="Фильтр",
        category=category,
        unit=Unit.objects.get(name="Штука"),
        recommended_price=Decimal("500"),
        tracking_mode=PartType.TrackingMode.BULK,
    )

    def lot(quantity=100, cost="160"):
        batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch,
            part_type=part,
            quantity=Decimal(str(quantity)),
            unit_cost_currency=Decimal(cost),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, admin)
        line.refresh_from_db()
        created = create_stock_lot(line, location, Decimal(str(quantity)))
        receive_stock_lot(created, by=admin)
        return created

    return {"part": part, "location": location, "lot": lot}


def _sale(stock, customer, admin, quantity=1, price="500"):
    sale = create_sale(customer=customer, by=admin)
    add_stock_lot_to_sale(
        sale, stock["lot"](), Decimal(str(quantity)), unit_price=Decimal(price), by=admin
    )
    return complete_sale(sale, by=admin)


def _repair(stock, customer, admin, quantity=1, snapshot=Decimal("500")):
    order = create_repair_order(customer=customer, by=admin)
    line = add_stock_lot_to_repair_order(
        order,
        stock["lot"](),
        Decimal(str(quantity)),
        customer_unit_price_rub=snapshot,
        by=admin,
    )
    return complete_repair_order(order, by=admin), line


def _period():
    return resolve_period({})


def _post_status(client, customer, period, paid=True):
    payload = {
        "customer_id": customer.pk,
        "date_from": period.date_from.isoformat(),
        "date_to": period.date_to.isoformat(),
    }
    if paid:
        payload["paid"] = "1"
    return client.post(reverse("reports_client_period_payment_status"), payload)


def test_acknowledgement_persists_and_get_never_writes(client, admin, stock):
    customer = Customer.objects.create(name="Иванов")
    _sale(stock, customer, admin, quantity=2, price="700")
    period = _period()
    client.login(username="admin", password=PASSWORD)

    before = CustomerPeriodPaymentAcknowledgement.objects.count()
    response = client.get(reverse("reports_clients_overview"))
    assert response.status_code == 200
    assert CustomerPeriodPaymentAcknowledgement.objects.count() == before
    assert "Оплатил" in response.content.decode()

    assert _post_status(client, customer, period).status_code == 302
    acknowledgement = CustomerPeriodPaymentAcknowledgement.objects.get()
    assert acknowledgement.amount_rub == Decimal("1400.00")
    assert acknowledgement.acknowledged_by == admin
    assert "checked" in client.get(reverse("reports_clients_overview")).content.decode()

    assert _post_status(client, customer, period, paid=False).status_code == 302
    acknowledgement.refresh_from_db()
    assert acknowledgement.revoked_at is not None


def test_returns_change_net_amount_and_make_payment_stale(client, admin, stock):
    customer = Customer.objects.create(name="Петров")
    sale = _sale(stock, customer, admin, quantity=5, price="100")
    repair, repair_line = _repair(stock, customer, admin, quantity=4, snapshot=Decimal("500"))
    period = _period()
    client.login(username="admin", password=PASSWORD)
    _post_status(client, customer, period)

    sale_return = create_return(source=sale, by=admin)
    add_sale_line_return(
        sale_return,
        sale.lines.get(),
        Decimal("2"),
        to_location=stock["location"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=admin,
    )
    complete_return(sale_return, by=admin)
    repair_return = create_return(source=repair, by=admin)
    add_repair_line_return(
        repair_return,
        repair_line,
        Decimal("1"),
        to_location=stock["location"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=admin,
    )
    complete_return(repair_return, by=admin)

    state = customer_payment_state(customer_id=customer.pk, period=period)
    assert state["amount"] == Decimal("1800.00")  # sale 3×100 + repair 3×500
    row = next(
        row for row in get_clients_sales_and_repairs(period) if row["customer_id"] == customer.pk
    )
    assert row["client_total_known"] == Decimal("1800.00")
    assert not payment_statuses_for_rows(rows=[row], period=period)[customer.pk]["paid"]

    cancel_return(repair_return, by=admin, reason="Ошибочный возврат")
    assert customer_payment_state(customer_id=customer.pk, period=period)["amount"] == Decimal(
        "2300.00"
    )


def test_frozen_repair_price_stays_paid_but_legacy_fallback_price_stales(client, admin, stock):
    customer = Customer.objects.create(name="Сидоров")
    _repair(stock, customer, admin, quantity=1, snapshot=Decimal("500"))
    legacy_order, legacy_line = _repair(stock, customer, admin, quantity=1, snapshot=Decimal("500"))
    legacy_line.customer_unit_price_rub = None
    legacy_line.save(update_fields=["customer_unit_price_rub"])
    period = _period()
    client.login(username="admin", password=PASSWORD)
    _post_status(client, customer, period)

    stock["part"].recommended_price = Decimal("700")
    stock["part"].save(update_fields=["recommended_price"])
    row = next(
        row for row in get_clients_sales_and_repairs(period) if row["customer_id"] == customer.pk
    )
    status = payment_statuses_for_rows(rows=[row], period=period)[customer.pk]
    assert status["amount"] == Decimal("1200.00")
    assert not status["paid"]

    # A separate client with only a real historical snapshot is unaffected by catalog edits.
    frozen_customer = Customer.objects.create(name="Исторический")
    _repair(stock, frozen_customer, admin, quantity=1, snapshot=Decimal("500"))
    _post_status(client, frozen_customer, period)
    frozen_row = next(
        row
        for row in get_clients_sales_and_repairs(period)
        if row["customer_id"] == frozen_customer.pk
    )
    assert payment_statuses_for_rows(rows=[frozen_row], period=period)[frozen_customer.pk]["paid"]


def test_period_customer_and_permission_boundaries(client, admin, manager, viewer, stock):
    first = Customer.objects.create(name="Первый")
    second = Customer.objects.create(name="Второй")
    _sale(stock, first, admin, price="400")
    _sale(stock, second, admin, price="400")
    period = _period()

    client.login(username="viewer", password=PASSWORD)
    assert _post_status(client, first, period).status_code == 403
    client.login(username="manager", password=PASSWORD)
    assert _post_status(client, first, period).status_code == 302
    assert CustomerPeriodPaymentAcknowledgement.objects.filter(customer=first).exists()
    assert not CustomerPeriodPaymentAcknowledgement.objects.filter(customer=second).exists()


def test_payment_status_preload_is_bounded_for_a_report_page(admin):
    customers = [Customer.objects.create(name=f"Клиент {number}") for number in range(50)]
    rows = [{"linked": True, "customer_id": customer.pk} for customer in customers]
    with CaptureQueriesContext(connection) as queries:
        statuses = payment_statuses_for_rows(rows=rows, period=_period())

    assert set(statuses) == {customer.pk for customer in customers}
    assert len(queries) <= 4
