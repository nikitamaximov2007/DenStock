"""Whole-document cancellation runs in its own transaction on PostgreSQL.

``cancel_sale`` and ``cancel_repair_order`` lock the document with
``select_for_update`` but were not atomic, and their views call them outside
any transaction (ATOMIC_REQUESTS is off). In real autocommit Django refuses
the lock: "Отменить продажу" and every repair-order cancel ended in HTTP 500.
The usual test transaction hid it; these tests run in autocommit.
"""
from decimal import Decimal

import pytest
from django.db import connection
from django.urls import reverse

from apps.inventory.models import StockLot, StockMovement
from apps.repairs.models import RepairOrder
from apps.repairs.services import cancel_repair_order, complete_repair_order
from apps.sales import services as sale_services
from apps.sales.models import Sale
from apps.sales.services import add_stock_lot_to_sale, cancel_sale, complete_sale
from tests import test_postgresql_concurrency_matrix as matrix

pytestmark = [
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="PostgreSQL autocommit integration test"
    ),
]

world = matrix.world
RETURNS = (StockMovement.MovementType.RETURN_LOT,)


def _completed_sale(world, qty="2"):
    lot = world["make_lot"](world["loc_a"], 5)
    sale = matrix._build_sale(lot.pk, world["admin"].pk, qty=qty)
    return complete_sale(sale, by=world["admin"]), lot


def _returns():
    return StockMovement.objects.filter(movement_type__in=RETURNS).count()


def test_sale_cancel_view_works_in_autocommit(client, world):
    assert connection.get_autocommit()
    sale, lot = _completed_sale(world)
    client.force_login(world["admin"])

    response = client.post(
        reverse("sale_cancel", args=[sale.pk]), {"reason": "ошибка", "author": "Оператор"}
    )

    assert response.status_code == 302
    sale.refresh_from_db()
    assert sale.status == Sale.Status.CANCELED
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("5")
    assert _returns() == 1


def test_cancel_sale_called_directly_in_autocommit(world):
    sale, lot = _completed_sale(world)

    cancel_sale(sale, by=world["admin"], reason="ошибка", author="Оператор")
    cancel_sale(sale, by=world["admin"], reason="ещё раз", author="Оператор")

    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.CANCELED
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("5")
    assert _returns() == 1


def test_sale_cancel_is_all_or_nothing(world, monkeypatch):
    lot_a = world["make_lot"](world["loc_a"], 5)
    lot_b = world["make_lot"](world["loc_b"], 5)
    sale = matrix._build_sale(lot_a.pk, world["admin"].pk, qty="1")
    add_stock_lot_to_sale(sale, lot_b, Decimal("1"), unit_price=Decimal("100"), by=world["admin"])
    complete_sale(sale, by=world["admin"])
    real_return = sale_services.return_stock_lot_quantity
    calls = []

    def second_return_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("сбой посреди отмены")
        return real_return(*args, **kwargs)

    monkeypatch.setattr(sale_services, "return_stock_lot_quantity", second_return_fails)
    with pytest.raises(RuntimeError):
        cancel_sale(sale, by=world["admin"], reason="ошибка", author="Оператор")

    assert len(calls) == 2
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    assert _returns() == 0
    assert StockLot.objects.get(pk=lot_a.pk).quantity == Decimal("4")
    assert StockLot.objects.get(pk=lot_b.pk).quantity == Decimal("4")


def test_repair_cancel_view_works_for_a_draft_and_a_completed_order(client, world):
    lot = world["make_lot"](world["loc_a"], 5)
    draft = matrix._build_repair(lot.pk, world["admin"].pk, qty="1")
    done = matrix._build_repair(lot.pk, world["admin"].pk, qty="2")
    complete_repair_order(done, by=world["admin"])
    client.force_login(world["admin"])

    for order in (draft, done):
        response = client.post(
            reverse("repair_order_cancel", args=[order.pk]),
            {"reason": "ошибка", "author": "Оператор"},
        )
        assert response.status_code == 302
        assert RepairOrder.objects.get(pk=order.pk).status == RepairOrder.Status.CANCELED
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("5")


def test_cancel_repair_order_called_directly_in_autocommit(world):
    lot = world["make_lot"](world["loc_a"], 5)
    order = matrix._build_repair(lot.pk, world["admin"].pk, qty="2")
    complete_repair_order(order, by=world["admin"])

    cancel_repair_order(order, by=world["admin"], reason="ошибка", author="Оператор")
    cancel_repair_order(order, by=world["admin"], reason="ещё раз", author="Оператор")

    assert RepairOrder.objects.get(pk=order.pk).status == RepairOrder.Status.CANCELED
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("5")
