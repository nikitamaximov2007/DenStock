"""Reservation -> Sale: one draft per reservation, never a silent 0 ₽ line.

`create_sale_from_reservation` resolved each line's price with
``... or Decimal("0")``, so a part without a current price became a 0 ₽ line
that looked like a legitimate sale. The "check the prices" message meant to
warn about it sat after a ``return`` and never ran. And the reservation stays
ACTIVE until the sale is completed, so every repeated "Продать из резерва"
created another independent draft for the same reservation.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Reservation, Sale, SaleLine
from apps.sales.services import (
    SaleError,
    activate_reservation,
    add_stock_lot_to_reservation,
    complete_sale,
    create_reservation,
    create_sale_from_reservation,
    get_or_create_sale_from_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location
from tests.customs_support import remember_customs

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="resv-admin", password=PASSWORD)


@pytest.fixture
def env(db, admin):
    return {
        "admin": admin,
        "supplier": Supplier.objects.create(name="ООО Поставка"),
        "category": Category.objects.create(name="Резерв"),
        "cell": get_or_create_location("S06-D03-C01", name="Ячейка"),
    }


def _part(env, *, name="РЕМЕНЬ", price="1000"):
    return PartType.objects.create(
        name=name, category=env["category"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=None if price is None else Decimal(price),
    )


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


def _reservation(env, *lots, quantity="2"):
    reservation = create_reservation(customer_name="Иванов", by=env["admin"])
    for lot in lots:
        add_stock_lot_to_reservation(reservation, lot, Decimal(quantity), by=env["admin"])
    return activate_reservation(reservation, by=env["admin"])


# --- Priced reservation -> exactly one correct draft --------------------------


def test_priced_reservation_becomes_one_draft_without_moving_stock(env):
    lot = _stock(env, _part(env))
    reservation = _reservation(env, lot)
    movements = StockMovement.objects.count()

    sale = create_sale_from_reservation(reservation, by=env["admin"])

    assert sale.status == Sale.Status.DRAFT
    assert sale.reservation_id == reservation.pk
    line = sale.lines.get()
    assert (line.quantity, line.unit_price, line.total_price) == (
        Decimal("2"), Decimal("1000"), Decimal("2000")
    )
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")
    assert StockMovement.objects.count() == movements


def test_retry_returns_the_same_draft(env):
    reservation = _reservation(env, _stock(env, _part(env)))

    first, created_first = get_or_create_sale_from_reservation(reservation, by=env["admin"])
    second, created_second = get_or_create_sale_from_reservation(reservation, by=env["admin"])

    assert (created_first, created_second) == (True, False)
    assert first.pk == second.pk
    assert Sale.objects.count() == 1
    assert SaleLine.objects.count() == 1


def test_double_submit_through_the_view_opens_the_same_draft(client, env):
    reservation = _reservation(env, _stock(env, _part(env)))
    client.force_login(env["admin"])
    url = reverse("sale_from_reservation", args=[reservation.pk])

    first = client.post(url)
    second = client.post(url, follow=True)

    assert Sale.objects.count() == 1
    sale = Sale.objects.get()
    assert first["Location"] == reverse("sale_detail", args=[sale.pk])
    assert "уже создана" in second.content.decode()


def test_success_message_is_actually_shown(client, env):
    reservation = _reservation(env, _stock(env, _part(env)))
    client.force_login(env["admin"])

    response = client.post(reverse("sale_from_reservation", args=[reservation.pk]), follow=True)

    assert "из резерва - проверьте цены" in response.content.decode()


# --- No automatic 0 ₽ fallback ------------------------------------------------


def test_unpriced_reservation_is_refused_not_turned_into_a_zero_sale(env):
    priced = _stock(env, _part(env, name="РЕМЕНЬ"))
    unpriced = _stock(env, _part(env, name="БЕЗ ЦЕНЫ", price=None))
    reservation = _reservation(env, priced, unpriced)

    with pytest.raises(SaleError, match="БЕЗ ЦЕНЫ"):
        create_sale_from_reservation(reservation, by=env["admin"])

    assert not Sale.objects.exists()
    assert not SaleLine.objects.filter(unit_price=0).exists()
    reservation.refresh_from_db()
    assert reservation.status == Reservation.Status.ACTIVE


def test_unpriced_refusal_is_shown_on_the_reservation_page(client, env):
    reservation = _reservation(env, _stock(env, _part(env, name="БЕЗ ЦЕНЫ", price=None)))
    client.force_login(env["admin"])

    response = client.post(reverse("sale_from_reservation", args=[reservation.pk]), follow=True)

    assert "не задана цена" in response.content.decode()
    assert not Sale.objects.exists()


def test_explicit_zero_card_price_is_still_a_deliberate_free_line(env):
    reservation = _reservation(env, _stock(env, _part(env, name="ПОДАРОК", price="0")))

    sale = create_sale_from_reservation(reservation, by=env["admin"])

    assert sale.lines.get().unit_price == Decimal("0")


# --- Completion still moves stock exactly once --------------------------------


def test_completion_moves_stock_once_and_closes_the_reservation(env):
    lot = _stock(env, _part(env))
    reservation = _reservation(env, lot)
    sale = create_sale_from_reservation(reservation, by=env["admin"])

    complete_sale(sale, by=env["admin"])

    lot.refresh_from_db()
    reservation.refresh_from_db()
    assert lot.quantity == Decimal("3")
    assert reservation.status == Reservation.Status.CONVERTED
    with pytest.raises(SaleError):
        create_sale_from_reservation(reservation, by=env["admin"])
    assert Sale.objects.count() == 1
