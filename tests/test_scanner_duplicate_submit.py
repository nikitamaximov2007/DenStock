"""Двойная отправка корзины сканера обязана быть незаметной.

Токен запроса существует ровно для этого: повторная отправка той же корзины
должна вернуть результат первой, а не ошибку. Проверка токена стояла ДО взятия
блокировки документа, поэтому спасала только от последовательного повтора. При
двойном нажатии на сенсорном экране оба запроса уходят одновременно, оба не
находят токен, и второй после блокировки получал «документ уже проведён».

Склад при этом не страдал (списание защищено блокировкой лотов), но смысл
токена терялся именно в том случае, ради которого он и добавлен.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.db import close_old_connections, connection

from apps.actions.cart import KIND_SALE, add_scan, complete_cart, open_cart
from apps.actions.models import WarehouseAction
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockLot
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
TOKEN = "scanner-double-tap-0001"


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
        batch=batch, part_type=part, quantity=Decimal("10"), unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("10"))
    receive_stock_lot(lot, by=admin)
    return {"admin": admin, "part": part, "location": location, "lot": lot}


def _cart_with_one_row(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["part"], data["location"], quantity=Decimal("3"), by=data["admin"])
    return cart


def _available(data):
    return sum(
        lot.quantity
        for lot in StockLot.objects.filter(
            part_type=data["part"], location=data["location"], status=StockLot.Status.AVAILABLE
        )
    )


# --- Последовательный повтор ------------------------------------------------------------


def test_sequential_resubmit_replays_instead_of_failing(data):
    """Браузер повторил уже прошедший запрос: пользователь видит тот же результат."""
    cart = _cart_with_one_row(data)
    before = _available(data)

    first = complete_cart(cart, customer_comment="Иванов", by=data["admin"], request_token=TOKEN)
    second = complete_cart(cart, customer_comment="Иванов", by=data["admin"], request_token=TOKEN)

    assert [a.pk for a in first] == [a.pk for a in second]
    assert _available(data) == before - Decimal("3"), "повтор списал товар второй раз"
    assert WarehouseAction.objects.filter(sale_id=cart.pk).count() == len(first)


def test_resubmit_without_token_is_refused_not_duplicated(data):
    """Без токена повтор обязан быть отклонён, а не провести документ дважды."""
    from apps.actions.services import ActionError

    cart = _cart_with_one_row(data)
    before = _available(data)
    complete_cart(cart, customer_comment="Иванов", by=data["admin"])
    with pytest.raises(ActionError):
        complete_cart(cart, customer_comment="Иванов", by=data["admin"])
    assert _available(data) == before - Decimal("3")


# --- Одновременная двойная отправка -----------------------------------------------------


def _complete_concurrently(cart_pk, user_pk, barrier):
    from django.contrib.auth import get_user_model

    close_old_connections()
    try:
        barrier.wait(15)
        return complete_cart(
            Sale.objects.get(pk=cart_pk),
            customer_comment="Иванов",
            by=get_user_model().objects.get(pk=user_pk),
            request_token=TOKEN,
        )
    except Exception as exc:  # noqa: BLE001 - тест проверяет точный исход гонки.
        return exc
    finally:
        close_old_connections()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL concurrency integration test"
)
def test_postgresql_concurrent_double_tap_is_transparent(data):
    """Два одновременных запроса с одним токеном: оба получают один результат."""
    cart = _cart_with_one_row(data)
    before = _available(data)
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_complete_concurrently, cart.pk, data["admin"].pk, barrier)
            for _ in range(2)
        ]
        results = [future.result() for future in futures]

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"одновременная двойная отправка вернула ошибку: {errors}"

    first, second = ({a.pk for a in results[0]}, {a.pk for a in results[1]})
    assert first == second, "запросы вернули разные наборы записей журнала"
    assert _available(data) == before - Decimal("3"), "товар списан дважды"
    assert Sale.objects.get(pk=cart.pk).status == Sale.Status.COMPLETED
    assert WarehouseAction.objects.filter(sale_id=cart.pk).count() == len(first)
    assert WarehouseAction.objects.filter(request_token=TOKEN).count() == 1
