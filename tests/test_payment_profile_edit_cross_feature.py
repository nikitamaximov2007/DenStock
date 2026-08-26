"""Правка профиля клиента и признание оплаты за период.

Признание оплаты закрывает ДЕНЬГИ периода: состав документов и суммы. Имя и
телефон карточки к деньгам не относятся - переименовать клиента или дописать
ему телефон можно в любой момент, и подтверждённая оплата от этого не
перестаёт быть подтверждённой.

А вот настоящее изменение начислений - новая продажа, новый ремонт с деталями
клиенту, возврат, отмена, смена суммы - обязано снимать актуальность: подпись
стояла под другой суммой.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.customers.models import Customer, CustomerPeriodPaymentAcknowledgement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.reports.payment_status import customer_payment_state
from apps.reports.services import resolve_period
from apps.returns.models import StockReturnLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="admin", password=PASSWORD)


@pytest.fixture
def stock(admin):
    supplier = Supplier.objects.create(name="Поставка")
    category = Category.objects.create(name="Расходники")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True
    )
    part = PartType.objects.create(
        name="Фильтр", category=category, unit=Unit.objects.get(name="Штука"),
        recommended_price=Decimal("500"), tracking_mode=PartType.TrackingMode.BULK,
    )

    def lot(quantity=100, cost="160"):
        batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch, part_type=part, quantity=Decimal(str(quantity)),
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


def _acknowledge(client, customer, period):
    return client.post(
        reverse("reports_client_period_payment_status"),
        {
            "customer_id": customer.pk,
            "date_from": period.date_from.isoformat(),
            "date_to": period.date_to.isoformat(),
            "paid": "1",
        },
    )


def _state(customer, period):
    return customer_payment_state(customer_id=customer.pk, period=period)


def _acknowledged(customer, period):
    """Действующее подтверждение и его актуальность по отпечатку."""
    row = CustomerPeriodPaymentAcknowledgement.objects.filter(
        customer=customer, period_start=period.date_from,
        period_end=period.date_to, revoked_at__isnull=True,
    ).order_by("-acknowledged_at", "-pk").first()
    if row is None:
        return None, False
    state = _state(customer, period)
    fresh = (
        row.billable_fingerprint == state["fingerprint"]
        and row.amount_rub == state["amount"]
    )
    return row, fresh


# --- Правка профиля признание не трогает ------------------------------------


def test_renaming_the_customer_keeps_the_acknowledgement_fresh(client, admin, stock):
    customer = Customer.objects.create(name="Иванов", phone="")
    _sale(stock, customer, admin, quantity=2, price="700")
    period = resolve_period({})
    client.force_login(admin)
    assert _acknowledge(client, customer, period).status_code in (200, 302)
    row, fresh = _acknowledged(customer, period)
    assert row is not None and fresh

    customer.name = "Иванов Пётр Сергеевич"
    customer.save(update_fields=["name"])

    same_row, still_fresh = _acknowledged(customer, period)
    assert same_row.pk == row.pk  # то же подтверждение, не пересозданное
    assert still_fresh  # переименование деньгами не является


def test_adding_a_phone_keeps_the_acknowledgement_fresh(client, admin, stock):
    customer = Customer.objects.create(name="Иванов")
    _sale(stock, customer, admin, quantity=1, price="500")
    period = resolve_period({})
    client.force_login(admin)
    _acknowledge(client, customer, period)
    row, _ = _acknowledged(customer, period)

    customer.phone = "+7 900 000-00-00"
    customer.save()
    customer.refresh_from_db()
    assert customer.phone_normalized  # служебная форма пересчиталась

    same_row, fresh = _acknowledged(customer, period)
    assert same_row.pk == row.pk
    assert fresh


def test_comment_edit_keeps_the_acknowledgement_fresh(client, admin, stock):
    customer = Customer.objects.create(name="Иванов")
    _sale(stock, customer, admin, quantity=1, price="500")
    period = resolve_period({})
    client.force_login(admin)
    _acknowledge(client, customer, period)
    row, _ = _acknowledged(customer, period)

    customer.comment = "Постоянный клиент, звонить после 18:00"
    customer.save(update_fields=["comment"])

    same_row, fresh = _acknowledged(customer, period)
    assert same_row.pk == row.pk
    assert fresh


# --- Настоящее изменение начислений признание снимает -----------------------


def test_a_new_sale_stales_the_acknowledgement(client, admin, stock):
    customer = Customer.objects.create(name="Иванов")
    _sale(stock, customer, admin, quantity=1, price="500")
    period = resolve_period({})
    client.force_login(admin)
    _acknowledge(client, customer, period)
    _, fresh = _acknowledged(customer, period)
    assert fresh

    _sale(stock, customer, admin, quantity=1, price="300")  # начисления выросли

    row, still_fresh = _acknowledged(customer, period)
    assert row is not None  # запись остаётся в журнале
    assert not still_fresh  # но она больше не покрывает текущую сумму
    assert row.amount_rub == Decimal("500.00")  # исходная сумма сохранена


def test_a_return_stales_the_acknowledgement(client, admin, stock):
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(stock, customer, admin, quantity=4, price="500")
    period = resolve_period({})
    client.force_login(admin)
    _acknowledge(client, customer, period)
    assert _acknowledged(customer, period)[1]

    returned = create_return(source=sale, by=admin)
    add_sale_line_return(
        returned, sale.lines.get(), Decimal("1"),
        to_location=stock["location"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE, by=admin,
    )
    complete_return(returned, by=admin)

    row, fresh = _acknowledged(customer, period)
    assert not fresh
    assert row.amount_rub == Decimal("2000.00")  # снимок исходной суммы цел


def test_profile_edit_and_billable_change_are_told_apart(client, admin, stock):
    """Обе правки подряд: снимает актуальность именно денежная."""
    customer = Customer.objects.create(name="Иванов")
    _sale(stock, customer, admin, quantity=1, price="500")
    period = resolve_period({})
    client.force_login(admin)
    _acknowledge(client, customer, period)

    customer.name = "Иванов И."
    customer.phone = "+7 900 111-22-33"
    customer.save()
    assert _acknowledged(customer, period)[1]  # профиль - не деньги

    _sale(stock, customer, admin, quantity=1, price="100")
    assert not _acknowledged(customer, period)[1]  # а это уже деньги


def test_seller_cannot_acknowledge_payment(client, admin, stock, django_user_model):
    customer = Customer.objects.create(name="Иванов")
    _sale(stock, customer, admin, quantity=1, price="500")
    period = resolve_period({})
    seller = django_user_model.objects.create_user(username="seller", password=PASSWORD)
    seller.groups.add(Group.objects.get(name=roles.SELLER))
    client.force_login(seller)
    response = _acknowledge(client, customer, period)
    assert response.status_code in (403, 302)
    assert not CustomerPeriodPaymentAcknowledgement.objects.filter(
        customer=customer, revoked_at__isnull=True
    ).exists()
