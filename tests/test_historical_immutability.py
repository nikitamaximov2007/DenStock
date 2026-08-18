"""Изменение справочника не должно переписывать уже проведённые документы.

Опасный класс дефекта: документ хранит снимок, но шаблон выводит живую карточку.
Тогда переименование клиента задним числом меняет вид старой продажи, и распечатка
годичной давности перестаёт совпадать с тем, что покажет система сегодня.

Здесь проверяется не только модель, но и то, что реально отрисовано на странице.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.actions.cart import KIND_REPAIR, KIND_SALE, add_scan, complete_cart
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import create_repair_order
from apps.sales.models import Sale
from apps.sales.services import create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
OLD_NAME = "Иванов Иван"
OLD_PHONE = "+7 912 111-11-11"
NEW_NAME = "Петров Пётр"
NEW_PHONE = "+7 999 999-99-99"


@pytest.fixture
def data(db, django_user_model):
    admin = django_user_model.objects.create_superuser(username="boss", password=PASSWORD)
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Болт", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value="700100", kind=PartNumber.Kind.OEM)
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("50"), unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("50"))
    receive_stock_lot(lot, by=admin)
    customer = Customer.objects.create(name=OLD_NAME, phone=OLD_PHONE)
    return {
        "admin": admin, "part": part, "location": location,
        "customer": customer, "lot": lot,
    }


def _rename(customer):
    customer.name = NEW_NAME
    customer.phone = NEW_PHONE
    customer.save()
    customer.refresh_from_db()


def _complete(data, kind):
    cart = (
        create_sale(customer=data["customer"], by=data["admin"])
        if kind == KIND_SALE
        else create_repair_order(customer=data["customer"], by=data["admin"])
    )
    add_scan(cart, data["part"], data["location"], quantity=Decimal("2"), by=data["admin"])
    complete_cart(cart, customer_comment=OLD_NAME, by=data["admin"])
    cart.refresh_from_db()
    return cart


# --- Модель ---------------------------------------------------------------------------


def test_sale_snapshot_survives_customer_rename(data):
    sale = _complete(data, KIND_SALE)
    _rename(data["customer"])
    sale.refresh_from_db()
    assert sale.customer_name == OLD_NAME, "переименование переписало историю продажи"
    assert sale.customer_id == data["customer"].pk, "связь с карточкой обязана сохраниться"


def test_repair_snapshot_survives_customer_rename(data):
    order = _complete(data, KIND_REPAIR)
    _rename(data["customer"])
    order.refresh_from_db()
    assert order.customer_name == OLD_NAME
    assert order.customer_id == data["customer"].pk


def test_frozen_money_survives_price_change(data):
    """Изменение рекомендованной цены не трогает деньги проведённого документа."""
    sale = _complete(data, KIND_SALE)
    revenue = Sale.objects.get(pk=sale.pk).revenue_total
    cost = Sale.objects.get(pk=sale.pk).cost_total

    part = data["part"]
    part.recommended_price = Decimal("99999")
    part.save(update_fields=["recommended_price"])

    fresh = Sale.objects.get(pk=sale.pk)
    assert fresh.revenue_total == revenue, "смена прайса переписала выручку старого документа"
    assert fresh.cost_total == cost, "смена прайса переписала себестоимость старого документа"


def test_part_rename_does_not_rewrite_action_journal(data):
    """Журнал действий хранит снимок наименования детали."""
    from apps.actions.models import WarehouseAction

    _complete(data, KIND_SALE)
    recorded = list(WarehouseAction.objects.values_list("part_name", flat=True))
    assert recorded and all(name == "Болт" for name in recorded)

    part = data["part"]
    part.name = "Болт переименованный"
    part.save(update_fields=["name"])

    after = list(WarehouseAction.objects.values_list("part_name", flat=True))
    assert after == recorded, "переименование детали переписало журнал"


# --- Отрисованная страница ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "route", "model"),
    [
        (KIND_SALE, "sale_detail", Sale),
        (KIND_REPAIR, "repair_order_detail", RepairOrder),
    ],
)
def test_document_page_shows_the_historical_name_not_the_live_card(
    client, data, kind, route, model
):
    """Главная проверка: на странице документа виден снимок, а не живая карточка."""
    document = _complete(data, kind)
    _rename(data["customer"])

    client.login(username="boss", password=PASSWORD)
    response = client.get(reverse(route, args=[document.pk]))
    assert response.status_code == 200
    body = response.content.decode()

    assert OLD_NAME in body, "страница документа потеряла историческое имя клиента"
    assert NEW_NAME not in body, (
        "страница документа показывает НОВОЕ имя карточки: живое обращение к "
        "справочнику переписывает историю визуально"
    )


def test_customer_card_itself_shows_the_current_name(client, data):
    """Контроль: карточка клиента обязана показывать актуальные данные."""
    _complete(data, KIND_SALE)
    _rename(data["customer"])

    client.login(username="boss", password=PASSWORD)
    response = client.get(reverse("customer_detail", args=[data["customer"].pk]))
    assert response.status_code == 200
    assert NEW_NAME in response.content.decode()
