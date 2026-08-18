"""Гонки на настоящем PostgreSQL по критическим складским операциям.

Обычные тесты идут в одной транзакции и одном соединении, поэтому конкурентную
работу двух операторов они не воспроизводят. Здесь каждый сценарий выполняется в
двух и более потоках с отдельными соединениями и барьером, чтобы запросы вошли в
операцию одновременно.

Проверяется главный складской инвариант: одновременная работа не создаёт
двойного списания, отрицательного остатка и потерянного обновления. Ожидаемый
исход гонки почти всегда один: ровно одна операция выигрывает, остальные
получают доменную ошибку, а остаток меняется ровно один раз.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.actions.services import ActionError, cancel_warehouse_action
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    adjust_stock_lot_quantity,
    create_stock_lot,
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
from apps.sales.models import Sale
from apps.sales.services import (
    SaleError,
    add_stock_lot_to_sale,
    complete_sale,
    create_sale,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

# Потокам нужны настоящие коммиты: под обычной тестовой транзакцией второе
# соединение не увидело бы данных первого. serialized_rollback возвращает
# посевные данные миграций, которые TRUNCATE стирает.
pytestmark = [
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL concurrency integration test",
    ),
]

PASSWORD = "parol-12345"


@pytest.fixture
def world():
    admin = get_user_model().objects.create_superuser(username="boss", password=PASSWORD)
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    loc_a = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    loc_b = StorageLocation.objects.create(
        name="Ячейка 2", code="S02-D01-C01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Болт", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value="700100", kind=PartNumber.Kind.OEM)

    def make_lot(location, qty):
        batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch, part_type=part, quantity=Decimal(str(qty)),
            unit_cost_currency=Decimal("10"),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, admin)
        line.refresh_from_db()
        lot = create_stock_lot(line, location, Decimal(str(qty)))
        receive_stock_lot(lot, by=admin)
        return lot

    return {
        "admin": admin, "part": part, "loc_a": loc_a, "loc_b": loc_b,
        "make_lot": make_lot, "lot": make_lot(loc_a, 1),
    }


def _available(part, location=None):
    qs = StockLot.objects.filter(part_type=part, status=StockLot.Status.AVAILABLE)
    if location is not None:
        qs = qs.filter(location=location)
    return sum(lot.quantity for lot in qs)


def _race(fn, *arg_tuples):
    """Запустить fn в нескольких потоках одновременно и вернуть все исходы."""
    barrier = Barrier(len(arg_tuples))

    def runner(args):
        close_old_connections()
        try:
            barrier.wait(20)
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - тест проверяет исход гонки
            return exc
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(arg_tuples)) as pool:
        futures = [pool.submit(runner, args) for args in arg_tuples]
        return [future.result() for future in futures]


def _wins_and_losses(results, error_types):
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, error_types)]
    unexpected = [
        r for r in results if isinstance(r, Exception) and not isinstance(r, error_types)
    ]
    assert not unexpected, f"неожиданная ошибка вместо доменной: {unexpected!r}"
    return wins, losses


def _user(pk):
    return get_user_model().objects.get(pk=pk)


# --- Продажа ------------------------------------------------------------------------------


def _build_sale(lot_pk, admin_pk, qty="1"):
    admin = _user(admin_pk)
    sale = create_sale(customer_name="Иванов", by=admin)
    add_stock_lot_to_sale(
        sale, StockLot.objects.get(pk=lot_pk), Decimal(qty),
        unit_price=Decimal("100"), by=admin,
    )
    return sale


def _complete_sale_by_pk(sale_pk, admin_pk):
    return complete_sale(Sale.objects.get(pk=sale_pk), by=_user(admin_pk))


def test_two_sales_cannot_both_take_the_last_unit(world):
    """Классический дефицит: один остаток, две продажи одновременно."""
    part, lot, admin = world["part"], world["lot"], world["admin"]
    first = _build_sale(lot.pk, admin.pk)
    second = _build_sale(lot.pk, admin.pk)
    assert _available(part) == Decimal("1")

    results = _race(_complete_sale_by_pk, (first.pk, admin.pk), (second.pk, admin.pk))
    wins, losses = _wins_and_losses(results, (SaleError, InventoryError))

    assert len(wins) == 1, "последний остаток продан дважды"
    assert len(losses) == 1
    assert _available(part) == Decimal("0")


def test_repeated_concurrent_completion_of_one_sale(world):
    """Двойное нажатие «Провести» по одному документу."""
    part, admin = world["part"], world["admin"]
    lot = world["make_lot"](world["loc_a"], 10)
    sale = _build_sale(lot.pk, admin.pk, qty="3")
    before = _available(part)

    results = _race(_complete_sale_by_pk, (sale.pk, admin.pk), (sale.pk, admin.pk))
    wins, _ = _wins_and_losses(results, (SaleError, InventoryError))

    assert _available(part) == before - Decimal("3"), "документ списал товар дважды"
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    assert len(wins) >= 1


def test_stock_never_goes_negative_under_parallel_pressure(world):
    """Четыре продажи по одной единице на остаток из двух."""
    part, admin = world["part"], world["admin"]
    lot = world["make_lot"](world["loc_b"], 2)
    sales = [_build_sale(lot.pk, admin.pk) for _ in range(4)]

    results = _race(_complete_sale_by_pk, *[(sale.pk, admin.pk) for sale in sales])
    wins, _ = _wins_and_losses(results, (SaleError, InventoryError, ActionError))

    assert len(wins) == 2, f"проведено {len(wins)} продаж при остатке 2"
    assert _available(part, world["loc_b"]) == Decimal("0")
    assert all(item.quantity >= 0 for item in StockLot.objects.all()), "отрицательный остаток"


# --- Ремонт -------------------------------------------------------------------------------


def _build_repair(lot_pk, admin_pk, qty="1"):
    admin = _user(admin_pk)
    order = create_repair_order(customer_name="Петров", by=admin)
    add_stock_lot_to_repair_order(
        order, StockLot.objects.get(pk=lot_pk), Decimal(qty), by=admin
    )
    return order


def _complete_repair_by_pk(order_pk, admin_pk):
    return complete_repair_order(RepairOrder.objects.get(pk=order_pk), by=_user(admin_pk))


def test_two_repairs_cannot_both_issue_the_last_unit(world):
    part, lot, admin = world["part"], world["lot"], world["admin"]
    first = _build_repair(lot.pk, admin.pk)
    second = _build_repair(lot.pk, admin.pk)

    results = _race(_complete_repair_by_pk, (first.pk, admin.pk), (second.pk, admin.pk))
    wins, losses = _wins_and_losses(results, (RepairError, InventoryError))

    assert len(wins) == 1, "последний остаток выдан в ремонт дважды"
    assert len(losses) == 1
    assert _available(part) == Decimal("0")


def test_repeated_concurrent_completion_of_one_repair(world):
    part, admin = world["part"], world["admin"]
    lot = world["make_lot"](world["loc_a"], 8)
    order = _build_repair(lot.pk, admin.pk, qty="2")
    before = _available(part)

    _race(_complete_repair_by_pk, (order.pk, admin.pk), (order.pk, admin.pk))

    assert _available(part) == before - Decimal("2"), "ремонт списал детали дважды"


def test_repair_completion_races_with_cancellation(world):
    part, admin = world["part"], world["admin"]
    lot = world["make_lot"](world["loc_a"], 6)
    order = _build_repair(lot.pk, admin.pk, qty="2")
    before = _available(part)

    def either(which, order_pk, admin_pk):
        obj = RepairOrder.objects.get(pk=order_pk)
        user = _user(admin_pk)
        return (
            complete_repair_order(obj, by=user)
            if which == "complete"
            else cancel_repair_order(obj, by=user)
        )

    results = _race(either, ("complete", order.pk, admin.pk), ("cancel", order.pk, admin.pk))
    _wins_and_losses(results, (RepairError, InventoryError))

    order.refresh_from_db()
    after = _available(part)
    if order.status == RepairOrder.Status.COMPLETED:
        assert after == before - Decimal("2")
    else:
        assert after == before


# --- Корректировки и отмена журнала --------------------------------------------------------


def test_concurrent_adjustments_do_not_lose_an_update(world):
    """Две корректировки одного лота одновременно: обе обязаны примениться."""
    part, admin = world["part"], world["admin"]
    lot = world["make_lot"](world["loc_a"], 10)
    before = _available(part)

    def adjust(lot_pk, admin_pk, delta):
        return adjust_stock_lot_quantity(
            StockLot.objects.get(pk=lot_pk), Decimal(delta),
            by=_user(admin_pk), comment="гонка",
        )

    results = _race(adjust, (lot.pk, admin.pk, "3"), (lot.pk, admin.pk, "5"))
    _wins_and_losses(results, (InventoryError,))

    assert _available(part) == before + Decimal("8"), "потеряно одно из обновлений"


def test_concurrent_cancellation_of_one_action_restores_stock_once(world):
    """Двойная отмена одной записи журнала не должна вернуть товар дважды."""
    from apps.actions.cart import KIND_SALE, add_scan, complete_cart, open_cart
    from apps.actions.models import WarehouseAction

    part, admin = world["part"], world["admin"]
    world["make_lot"](world["loc_a"], 9)
    before = _available(part)
    cart = open_cart(KIND_SALE, by=admin)
    add_scan(cart, part, world["loc_a"], quantity=Decimal("4"), by=admin)
    actions = complete_cart(cart, customer_comment="Иванов", by=admin)
    assert _available(part) == before - Decimal("4")

    def cancel(action_pk, admin_pk):
        return cancel_warehouse_action(
            WarehouseAction.objects.get(pk=action_pk), by=_user(admin_pk), reason="гонка"
        )

    _race(cancel, (actions[0].pk, admin.pk), (actions[0].pk, admin.pk))

    assert _available(part) == before, "отмена вернула товар дважды"
    assert StockMovement.objects.filter(part_type=part).exists()
