"""Число запросов на списках и отчётах не должно расти вместе с данными.

Это защита от N+1, а не замер скорости. Проверяется свойство, а не конкретное
число: страница собирается за одно и то же количество запросов при малом и при
втрое большем объёме данных. Именно так выглядит регрессия N+1, когда кто-то
добавляет обращение к связанному объекту внутри цикла шаблона.

Точные числа намеренно не закрепляются: они законно меняются при рефакторинге,
а вот зависимость от объёма данных законной не бывает.
"""
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.actions.cart import KIND_REPAIR, KIND_SALE, add_scan, complete_cart, open_cart
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
SMALL = 4
LARGE = 12

PAGES = [
    "part_list",
    "lot_list",
    "item_list",
    "balance_list",
    "movement_list",
    "customer_list",
    "sale_list",
    "repair_order_list",
    "batch_list",
    "reports_stock",
    "reports_clients_overview",
    "reports_sales_by_client",
    "reports_repairs_by_client",
    "catalog_import_list",
]


def _seed(django_user_model, customers):
    """Создать склад и указанное число клиентов с продажей и ремонтом у каждого."""
    admin = django_user_model.objects.filter(username="boss").first()
    if admin is None:
        admin = django_user_model.objects.create_superuser(username="boss", password=PASSWORD)
        supplier = Supplier.objects.create(name="ООО Поставка")
        category = Category.objects.create(name="Вариатор")
        unit = Unit.objects.get(name="Штука")
        location = StorageLocation.objects.create(
            name="Ячейка 1", code="S01-D01-C01", storage_allowed=True, is_active=True
        )
        for index in range(3):
            part = PartType.objects.create(
                name=f"Деталь {index}", category=category, unit=unit,
                tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
            )
            PartNumber.objects.create(
                part=part, value=f"7001{index:04d}", kind=PartNumber.Kind.OEM
            )
            batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
            line = BatchLine.objects.create(
                batch=batch, part_type=part, quantity=Decimal("400"),
                unit_cost_currency=Decimal("10"),
            )
            batch.status = Batch.Status.ACCEPTED
            batch.save(update_fields=["status"])
            finalize_cost(batch, admin)
            line.refresh_from_db()
            lot = create_stock_lot(line, location, Decimal("400"))
            receive_stock_lot(lot, by=admin)

    location = StorageLocation.objects.first()
    parts = list(PartType.objects.all())
    existing = Customer.objects.count()
    for index in range(existing, customers):
        customer = Customer.objects.create(name=f"Клиент {index}", phone=f"+7912000{index:04d}")
        for kind in (KIND_SALE, KIND_REPAIR):
            cart = open_cart(kind, by=admin)
            for part in parts[: (index % 3) + 1]:
                add_scan(cart, part, location, quantity=Decimal("1"), by=admin)
            complete_cart(cart, customer_comment=customer.name, by=admin)
    return admin


def _count_queries(client, name):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(reverse(name))
    assert response.status_code == 200, f"{name} ответил {response.status_code}"
    return len(ctx)


@pytest.mark.parametrize("name", PAGES)
def test_query_count_does_not_grow_with_data(client, db, django_user_model, name):
    _seed(django_user_model, SMALL)
    client.login(username="boss", password=PASSWORD)
    small = _count_queries(client, name)

    _seed(django_user_model, LARGE)
    large = _count_queries(client, name)

    assert large == small, (
        f"{name}: запросов стало {large} против {small} при росте данных в три раза, "
        "это признак N+1"
    )


def test_seed_actually_produces_documents(client, db, django_user_model):
    """Контроль осмысленности: без документов проверка выше ничего бы не значила."""
    from apps.repairs.models import RepairOrder
    from apps.sales.models import Sale

    _seed(django_user_model, LARGE)
    assert Customer.objects.count() == LARGE
    assert Sale.objects.count() >= LARGE
    assert RepairOrder.objects.count() >= LARGE
