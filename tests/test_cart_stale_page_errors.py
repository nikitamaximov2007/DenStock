"""Устаревшая страница корзины не должна давать аварию вместо сообщения.

Сценарий склада: документ проведён другим путём (со страницы продажи или другим
оператором), а у сотрудника открыта прежняя страница сканера. Любое действие с
такой корзиной обязано закончиться понятным сообщением и возвратом на экран
сканера, а не страницей ошибки.

Проверено: защита есть и работает. Сессионный резолвер корзины намеренно не
возвращает уже проведённый документ, поэтому доменная ошибка «корзину менять
нельзя» до представления не доходит. Дефекта нет.

Тесты оставлены как защита от регрессии: в представлении ветка «убрать позицию»
вызывает сервис без перехвата доменной ошибки, в отличие от соседней ветки
«изменить количество». Пока резолвер отсекает непроектные состояния, это
безопасно; если его поведение изменят, эти тесты упадут раньше пользователя.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.actions.cart import KIND_SALE, add_scan, open_cart
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.services import complete_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


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
        batch=batch, part_type=part, quantity=Decimal("20"), unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("20"))
    receive_stock_lot(lot, by=admin)
    return {"admin": admin, "part": part, "location": location}


def _stale_cart_session(client, data):
    """Корзина в сессии, документ уже проведён другим путём."""
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["part"], data["location"], quantity=Decimal("2"), by=data["admin"])
    session = client.session
    session["actions_cart_sale"] = cart.pk
    session.save()
    complete_sale(cart, by=data["admin"])
    return cart


@pytest.mark.parametrize("operation", ["remove", "set"])
def test_stale_cart_action_answers_with_a_message_not_a_crash(client, data, operation):
    client.login(username="boss", password=PASSWORD)
    _stale_cart_session(client, data)
    row_key = f"{data['part'].pk}:{data['location'].pk}"

    response = client.post(
        reverse("actions_cart_update"),
        {"kind": KIND_SALE, "operation": operation, "row_key": row_key, "quantity": "1"},
        follow=True,
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "проведён" in body or "пуста" in body, (
        "устаревшая корзина не объяснила пользователю, что документ уже проведён"
    )


def test_stale_cart_does_not_change_stock(client, data):
    from apps.inventory.models import StockLot

    client.login(username="boss", password=PASSWORD)
    _stale_cart_session(client, data)
    before = sum(
        lot.quantity
        for lot in StockLot.objects.filter(status=StockLot.Status.AVAILABLE)
    )
    row_key = f"{data['part'].pk}:{data['location'].pk}"

    client.post(
        reverse("actions_cart_update"),
        {"kind": KIND_SALE, "operation": "remove", "row_key": row_key},
        follow=True,
    )

    after = sum(
        lot.quantity
        for lot in StockLot.objects.filter(status=StockLot.Status.AVAILABLE)
    )
    assert after == before, "действие с устаревшей корзиной изменило остаток"
