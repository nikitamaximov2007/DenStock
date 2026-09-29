"""Cancelling one Quick Actions row cancels the whole sale - and says so.

A multi-line scanner sale is one Sale document with one WarehouseAction per
row. "Отменить" on any row intentionally cancels the WHOLE document and
returns every line. The confirmation used to show only the selected row, and
the success message reported only that row's quantity and cell, so the
operator could believe a single part was cancelled. The behavior is kept;
the screen and the message now describe the whole document truthfully.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.actions.cart import add_scan, complete_cart, open_cart
from apps.actions.models import WarehouseAction
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Sale
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location
from tests.customs_support import remember_cart_customs, remember_customs

PASSWORD = "parol-12345"


@pytest.fixture
def env(db, django_user_model):
    Group.objects.all()
    admin = django_user_model.objects.create_superuser(username="qa-admin", password=PASSWORD)
    manager = django_user_model.objects.create_user(username="qa-boss", password=PASSWORD)
    manager.groups.add(Group.objects.get(name=roles.MANAGER))
    return {
        "admin": admin, "manager": manager,
        "supplier": Supplier.objects.create(name="Поставщик"),
        "category": Category.objects.create(name="Отмена"),
        "cells": [get_or_create_location(f"S09-D02-C0{i}", name=f"Ячейка {i}") for i in (1, 2, 3)],
    }


def _part_in_cell(env, article, cell, quantity="5"):
    part = PartType.objects.create(
        name=f"Деталь {article}", category=env["category"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(
        part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    remember_customs(part)
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(quantity), unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    receive_stock_lot(create_stock_lot(line, cell, Decimal(quantity)), by=env["admin"])
    return part


def _cart_sale(env, rows):
    cart = open_cart("sale", by=env["admin"])
    for part, cell, qty in rows:
        add_scan(cart, part, cell, quantity=Decimal(qty), by=env["admin"])
    remember_cart_customs(cart)
    actions = complete_cart(cart, customer=Customer.objects.create(name="Иванов"), by=env["admin"])
    return cart, actions


@pytest.fixture
def multi(env):
    cells = env["cells"]
    parts = [_part_in_cell(env, f"ART-{i}", cells[i]) for i in range(3)]
    sale, actions = _cart_sale(env, [(parts[0], cells[0], "1"), (parts[1], cells[1], "2"),
                                     (parts[2], cells[2], "3")])
    return {"parts": parts, "sale": sale, "actions": actions}


def test_multi_line_confirmation_lists_every_position_and_says_whole_sale(client, env, multi):
    client.force_login(env["manager"])
    selected = multi["actions"][1]

    body = client.get(reverse("actions_cancel", args=[selected.pk])).content.decode()

    assert "Отменится ВСЯ продажа" in body
    assert "Отменить всю продажу" in body
    for article, cell in zip(("ART-0", "ART-1", "ART-2"), env["cells"], strict=True):
        assert article in body
        assert cell.short_code in body
    # the existing line-level workflow is offered instead of implying it
    for line in multi["sale"].lines.all():
        assert reverse("sale_line_cancel", args=[line.pk]) in body


def test_multi_line_cancel_returns_everything_once_and_says_so(client, env, multi):
    client.force_login(env["manager"])
    selected = multi["actions"][0]

    response = client.post(
        reverse("actions_cancel", args=[selected.pk]), {"reason": "ошибка"}, follow=True
    )

    body = response.content.decode()
    assert "Продажа отменена целиком" in body
    for cell in env["cells"]:
        assert cell.short_code in body
    multi["sale"].refresh_from_db()
    assert multi["sale"].status == Sale.Status.VOIDED
    assert set(
        WarehouseAction.objects.filter(sale=multi["sale"]).values_list("status", flat=True)
    ) == {WarehouseAction.Status.CANCELLED}
    for part in multi["parts"]:
        assert StockLot.objects.get(part_type=part).quantity == Decimal("5")
    returns = StockMovement.objects.filter(movement_type=StockMovement.MovementType.RETURN_LOT)
    assert returns.count() == 3

    # a second submit changes nothing: no duplicate stock return
    client.post(reverse("actions_cancel", args=[selected.pk]), {"reason": "ещё раз"})
    assert returns.count() == 3
    for part in multi["parts"]:
        assert StockLot.objects.get(part_type=part).quantity == Decimal("5")


def test_single_line_confirmation_stays_simple(client, env):
    part = _part_in_cell(env, "ONE-1", env["cells"][0])
    _sale, actions = _cart_sale(env, [(part, env["cells"][0], "2")])
    client.force_login(env["manager"])

    body = client.get(reverse("actions_cancel", args=[actions[0].pk])).content.decode()
    assert "ВСЯ продажа" not in body
    assert "Подтвердить отмену" in body
    assert "ONE-1" in body and env["cells"][0].short_code in body

    response = client.post(
        reverse("actions_cancel", args=[actions[0].pk]), {"reason": "дубль"}, follow=True
    )
    assert "Продажа отменена (" in response.content.decode()
    assert f"возвращён в ячейку {env['cells'][0].short_code}" in response.content.decode()
    assert StockLot.objects.get(part_type=part).quantity == Decimal("5")
