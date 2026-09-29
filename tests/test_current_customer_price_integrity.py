"""A negative current customer price is never accepted and never charged.

`PartType.recommended_price` had no lower bound: the card edit form stored a
negative value, and `resolve_effective_inventory_customer_price` passed it
straight through. `check_sale_line_price` only rejected ``None`` and stale
zeros, so a Quick Actions sale (and a repair line) booked NEGATIVE revenue
from a negative card price. The card now refuses negative prices, the shared
resolver turns a negative price into "not set", and the sale/repair services
refuse negative money as a backstop.

The two existing zero rules are kept exactly: an explicit zero in the card is
a deliberate free sale in Quick Actions, while the public catalog treats a
non-positive price as unknown ("Уточнить цену").
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.cart import add_scan, complete_cart, open_cart
from apps.actions.services import ActionError, perform_action
from apps.catalog.forms import PartTypeForm
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.catalog.public_contracts import resolve_current_customer_price as public_price
from apps.customers.models import Customer
from apps.inventory.models import StockMovement
from apps.inventory.pricing import resolve_effective_inventory_customer_price
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    RepairError,
    add_stock_lot_to_repair_order,
    create_repair_order,
)
from apps.sales.models import Sale, SaleLine
from apps.sales.services import (
    SaleError,
    activate_reservation,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    complete_sale,
    create_reservation,
    create_sale,
    create_sale_from_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location
from tests.customs_support import remember_cart_customs, remember_customs

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="price-admin", password=PASSWORD)


@pytest.fixture
def env(db, admin):
    return {
        "admin": admin,
        "supplier": Supplier.objects.create(name="ООО Поставка"),
        "category": Category.objects.create(name="Цены"),
        "cell": get_or_create_location("S06-D02-C01", name="Ячейка"),
    }


def _part(env, *, name="ПОРШЕНЬ", article="PR-1", price="1000"):
    part = PartType.objects.create(
        name=name, category=env["category"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=None if price is None else Decimal(price),
    )
    PartNumber.objects.create(
        part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    return part


def _poison_price(part, price):
    """A negative price that reached the DB through a path without validation."""
    PartType.objects.filter(pk=part.pk).update(recommended_price=Decimal(price))
    part.refresh_from_db()
    return part


def _stock(env, part, quantity="5"):
    remember_customs(part)
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(quantity), unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["cell"], Decimal(quantity))
    return receive_stock_lot(lot, by=env["admin"])


def _form_data(part, **overrides):
    data = {
        "name": part.name, "category": part.category_id, "unit": part.unit_id,
        "tracking_mode": part.tracking_mode, "description": "",
        "recommended_price": "1000", "min_price": "", "min_stock_level": "0",
    }
    data.update(overrides)
    return data


# --- A. The card refuses a negative current price -----------------------------


def test_part_form_rejects_a_negative_current_price(env):
    part = _part(env)
    form = PartTypeForm(_form_data(part, recommended_price="-500"), instance=part)
    assert not form.is_valid()
    assert "recommended_price" in form.errors


def test_part_form_rejects_a_negative_min_price(env):
    part = _part(env)
    form = PartTypeForm(_form_data(part, min_price="-1"), instance=part)
    assert not form.is_valid()
    assert "min_price" in form.errors


def test_crafted_edit_post_with_a_negative_price_changes_nothing(client, env):
    part = _part(env)
    client.force_login(env["admin"])

    response = client.post(
        reverse("part_edit", args=[part.pk]), _form_data(part, recommended_price="-500")
    )

    assert response.status_code == 200  # form re-rendered with the error
    part.refresh_from_db()
    assert part.recommended_price == Decimal("1000")


def test_zero_and_positive_prices_are_still_accepted_by_the_card(env):
    part = _part(env)
    for value in ("0", "1234.50"):
        form = PartTypeForm(_form_data(part, recommended_price=value), instance=part)
        assert form.is_valid(), form.errors


# --- B. No automated path books a negative customer charge --------------------


def test_resolver_turns_a_negative_price_into_not_set_and_keeps_zero():
    assert resolve_effective_inventory_customer_price(None, Decimal("-1")) is None
    assert resolve_effective_inventory_customer_price(None, Decimal("0")) == Decimal("0")
    assert resolve_effective_inventory_customer_price(None, Decimal("10")) == Decimal("10")
    assert resolve_effective_inventory_customer_price(None, None) is None


def test_quick_sale_refuses_a_negative_card_price_and_leaves_stock(env):
    part = _poison_price(_part(env), "-500")
    lot = _stock(env, part)
    movements = StockMovement.objects.count()
    cart = open_cart("sale", by=env["admin"])

    with pytest.raises(ActionError):
        add_scan(cart, part, env["cell"], quantity=Decimal("1"), by=env["admin"])

    assert SaleLine.objects.count() == 0
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")
    assert StockMovement.objects.count() == movements


def test_scanner_action_refuses_a_negative_card_price(env):
    part = _poison_price(_part(env), "-500")
    _stock(env, part)

    with pytest.raises(ActionError):
        perform_action(
            part=part, location=env["cell"], action_type="sale",
            quantity=Decimal("1"), customer_comment="Иванов", by=env["admin"],
        )

    assert not SaleLine.objects.filter(unit_price__lt=0).exists()
    assert Sale.objects.filter(status=Sale.Status.COMPLETED).count() == 0


def test_explicit_zero_in_the_card_is_still_a_free_quick_sale(env):
    part = _part(env, name="ПОДАРОК", article="ZERO-2", price="0")
    _stock(env, part)
    cart = open_cart("sale", by=env["admin"])

    row = add_scan(cart, part, env["cell"], quantity=Decimal("1"), by=env["admin"])

    assert row.unit_price == Decimal("0")
    remember_cart_customs(cart)
    complete_cart(cart, customer=Customer.objects.create(name="Иванов"), by=env["admin"])
    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED


def test_sale_service_refuses_negative_money_from_any_caller(env):
    lot = _stock(env, _part(env))
    sale = create_sale(customer_name="Иванов", by=env["admin"])

    with pytest.raises(SaleError):
        add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("-1"), by=env["admin"])

    assert not SaleLine.objects.exists()


def test_complete_sale_refuses_a_draft_with_a_negative_line(env):
    part = _part(env)
    lot = _stock(env, part)
    sale = create_sale(customer_name="Иванов", by=env["admin"])
    # A draft written by a path without the service guard (or before it).
    SaleLine.objects.create(
        sale=sale, part_type=part, stock_lot=lot, batch=lot.batch, batch_line=lot.batch_line,
        quantity=Decimal("1"), unit_price=Decimal("-500"), total_price=Decimal("-500"),
    )

    with pytest.raises(SaleError):
        complete_sale(sale, by=env["admin"])

    sale.refresh_from_db()
    lot.refresh_from_db()
    assert sale.status == Sale.Status.DRAFT
    assert lot.quantity == Decimal("5")


def test_sale_from_reservation_never_books_negative_revenue(env):
    part = _part(env)
    lot = _stock(env, part)
    reservation = create_reservation(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_reservation(reservation, lot, Decimal("1"), by=env["admin"])
    reservation = activate_reservation(reservation, by=env["admin"])
    _poison_price(part, "-500")

    sale = create_sale_from_reservation(reservation, by=env["admin"])

    assert all(line.unit_price >= 0 for line in sale.lines.all())


def test_repair_default_price_is_not_set_for_a_negative_card_price(env):
    part = _poison_price(_part(env), "-500")
    lot = _stock(env, part)
    order = create_repair_order(customer_name="Иванов", by=env["admin"])

    line = add_stock_lot_to_repair_order(order, lot, Decimal("1"), by=env["admin"])

    assert line.customer_unit_price_rub is None


def test_repair_explicit_negative_price_is_refused(env):
    lot = _stock(env, _part(env))
    order = create_repair_order(customer_name="Иванов", by=env["admin"])

    with pytest.raises(RepairError):
        add_stock_lot_to_repair_order(
            order, lot, Decimal("1"), customer_unit_price_rub=Decimal("-10"), by=env["admin"]
        )


# --- C. PRO-STOR never shows a negative or zero price -------------------------


@pytest.mark.parametrize("price", ["-500", "0"])
def test_public_catalog_shows_clarify_for_non_positive_prices(env, price):
    part = _poison_price(_part(env), price)
    projected = public_price(part)
    assert projected.price_rub is None
    assert projected.status == "clarify"


# --- D/E. History and manual overrides ----------------------------------------


def test_completed_sale_line_keeps_its_historical_price(env):
    part = _part(env)
    lot = _stock(env, part)
    sale = create_sale(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("1000"), by=env["admin"])
    complete_sale(sale, by=env["admin"])

    _poison_price(part, "-500")

    line = SaleLine.objects.get(sale=sale)
    assert line.unit_price == Decimal("1000")
    assert line.total_price == Decimal("1000")


def test_manual_sale_price_override_still_works(env):
    lot = _stock(env, _part(env))
    sale = create_sale(customer_name="Иванов", by=env["admin"])

    line = add_stock_lot_to_sale(
        sale, lot, Decimal("1"), unit_price=Decimal("777"), by=env["admin"]
    )
    free = add_stock_lot_to_sale(
        sale, lot, Decimal("1"), unit_price=Decimal("0"), by=env["admin"]
    )

    assert (line.unit_price, free.unit_price) == (Decimal("777"), Decimal("0"))
