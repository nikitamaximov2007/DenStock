"""Anonymous cart: server-authoritative, tamper-proof and write-free.

The cart lives only in the customer's signed cookie. It never trusts a client
price or quantity beyond the checks below, and it never creates a
reservation, a sale or any database row.
"""

from decimal import Decimal

import pytest
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.core import signing

from apps.catalog.public_cart import (
    CART_SESSION_KEY,
    MAX_CART_LINES,
    MAX_CART_QUANTITY,
    read_cart,
)
from apps.inventory.availability import available_totals
from apps.sales.models import Reservation, Sale
from tests.public_catalog_support import assert_no_writes, capture


def _add(client, part, quantity="1", **extra):
    return client.post(f"/cart/{part.public_id}/add/", {"quantity": quantity, **extra})


def _cart(client):
    return client.get("/cart/").content.decode()


def _set_cookie_cart(client, value):
    store = SessionStore()
    store[CART_SESSION_KEY] = value
    store.save()
    client.cookies["prostor_cart"] = store.session_key


def test_add_update_remove_round_trip(public_client, public_catalog):
    part = public_catalog.part("Clutch kit", article="CK-1", price="4200")
    public_catalog.stock(part, "5")

    assert _add(public_client, part, "2").status_code == 302
    body = _cart(public_client)
    assert "Clutch kit" in body and "CK-1" in body
    assert "8\u00a0400\u00a0₽" in body, "line total and cart total"

    _add(public_client, part, "3", next="cart")
    assert 'value="3"' in _cart(public_client)

    public_client.post(f"/cart/{part.public_id}/remove/")
    assert "Корзина пуста" in _cart(public_client)


def test_client_price_and_extra_fields_are_ignored(public_client, public_catalog):
    part = public_catalog.part("Brake disc", article="BD-1", price="7000")
    public_catalog.stock(part, "2")

    _add(public_client, part, "1", price="1", price_rub="1", total="1", available="999")

    body = _cart(public_client)
    assert "7\u00a0000\u00a0₽" in body
    assert "1\u00a0₽" not in body


@pytest.mark.parametrize("quantity", ["0", "-1", "abc", "1.5", "", " ", "1e3", "0x10", "٣"])
def test_invalid_quantities_change_nothing(public_client, public_catalog, quantity):
    part = public_catalog.part("Spark plug", article="SP-9")
    public_catalog.stock(part, "5")

    response = _add(public_client, part, quantity)
    assert response.status_code == 302
    assert "Корзина пуста" in _cart(public_client)


def test_huge_quantity_is_refused(public_client, public_catalog):
    part = public_catalog.part("Washer", article="W-1")
    for quantity in (str(MAX_CART_QUANTITY + 1), "9" * 50):
        _add(public_client, part, quantity)
    assert "Корзина пуста" in _cart(public_client)


def test_quantity_above_availability_is_refused_with_a_reason(public_client, public_catalog):
    part = public_catalog.part("Piston", article="P-1")
    public_catalog.stock(part, "3")

    response = _add(public_client, part, "4")
    follow = public_client.get(response["Location"]).content.decode()

    assert "Piston: Сейчас доступно 3 шт. Уменьшите количество." in follow
    assert "Корзина пуста" in _cart(public_client)


def test_zero_stock_becomes_a_supply_inquiry(public_client, public_catalog):
    part = public_catalog.part("Rare gasket", article="RG-1", price="300")

    _add(public_client, part, "2")
    body = _cart(public_client)

    assert "Нет на складе: запрос о поставке" in body
    assert "Запрос о поставке: 1 поз." in body
    assert "Сумма по деталям в наличии" not in body, "an inquiry is not priced into the total"


def test_card_button_never_overwrites_a_chosen_quantity(public_client, public_catalog):
    part = public_catalog.part("Oil", article="OIL-1")
    public_catalog.stock(part, "10")
    _add(public_client, part, "4")

    _add(public_client, part, "1", if_absent="1")

    assert read_cart(public_client.session)[str(part.public_id)] == 4


def test_unknown_and_hidden_public_ids_are_404(public_client, public_catalog):
    hidden = public_catalog.part("Hidden", article="HID-9", public=False)
    assert _add(public_client, hidden).status_code == 404
    response = public_client.post("/cart/00000000-0000-0000-0000-000000000000/add/")
    assert response.status_code == 404
    assert public_client.post(f"/cart/{hidden.pk}/add/").status_code == 404


def test_line_count_is_capped(public_client, public_catalog):
    parts = [
        public_catalog.part(f"Cap part {index}", article=f"CAP-{index}") for index in range(51)
    ]
    for part in parts[:MAX_CART_LINES]:
        _add(public_client, part)
    response = _add(public_client, parts[-1])
    follow = public_client.get(response["Location"]).content.decode()

    assert len(read_cart(public_client.session)) == MAX_CART_LINES
    assert f"В корзине может быть не больше {MAX_CART_LINES} позиций." in follow


def test_offsite_next_is_never_followed(public_client, public_catalog):
    part = public_catalog.part("Belt", article="BLT-1")
    public_catalog.stock(part, "1")
    for target in ("https://evil.example/", "//evil.example/search/", "/admin/", "javascript:x"):
        response = _add(public_client, part, "1", next=target)
        assert response["Location"] == "/cart/", target


def test_corrupt_cookie_is_an_empty_cart(public_client, public_catalog):
    public_client.cookies["prostor_cart"] = "not-a-signed-value"
    assert "Корзина пуста" in _cart(public_client)
    forged = signing.dumps({CART_SESSION_KEY: {"x": 1}}, key="wrong-key", compress=True)
    public_client.cookies["prostor_cart"] = forged
    assert "Корзина пуста" in _cart(public_client)


@pytest.mark.parametrize(
    "stored",
    [
        "not a dict",
        ["list"],
        {"not-a-uuid": 1},
        {"00000000-0000-0000-0000-000000000000": 1},
        {"PLACEHOLDER": "5"},
        {"PLACEHOLDER": 0},
        {"PLACEHOLDER": 10**9},
        {"PLACEHOLDER": True},
    ],
)
def test_malformed_signed_cart_content_is_sanitized(public_client, public_catalog, stored):
    part = public_catalog.part("Sanitized", article="SAN-1")
    public_catalog.stock(part, "1")
    if isinstance(stored, dict):
        stored = {
            (str(part.public_id) if key == "PLACEHOLDER" else key): value
            for key, value in stored.items()
        }
    _set_cookie_cart(public_client, stored)

    response = public_client.get("/cart/")

    assert response.status_code == 200
    assert "Корзина пуста" in response.content.decode()


def test_parts_that_left_the_catalog_are_pruned_with_a_notice(public_client, public_catalog):
    part = public_catalog.part("Leaving part", article="LP-1")
    public_catalog.stock(part, "1")
    _add(public_client, part)
    assert "Leaving part" in _cart(public_client)
    part.is_public = False
    part.save(update_fields=["is_public"])

    body = _cart(public_client)

    assert "Некоторых деталей больше нет в каталоге" in body
    assert "Leaving part" not in body
    assert read_cart(public_client.session) == {}


def test_cart_recomputes_price_and_availability_every_time(public_client, public_catalog):
    part = public_catalog.part("Live part", article="LIVE-1", price="100")
    public_catalog.stock(part, "3")
    _add(public_client, part, "3")

    part.recommended_price = Decimal("150")
    part.save(update_fields=["recommended_price"])
    body = _cart(public_client)
    assert "450\u00a0₽" in body

    from apps.inventory.models import StockLot
    from apps.sales.services import (
        activate_reservation,
        add_stock_lot_to_reservation,
        create_reservation,
    )

    lot = StockLot.objects.get(part_type=part)
    reservation = create_reservation(customer_name="Walk-in", by=public_catalog.user)
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=public_catalog.user)
    activate_reservation(reservation, by=public_catalog.user)

    body = _cart(public_client)
    assert "Сейчас доступно только 1 шт. Уменьшите количество." in body
    assert "выбрано больше, чем есть сейчас" in body


def test_cart_operations_write_nothing_to_the_database(public_client, public_catalog):
    part = public_catalog.part("No write part", article="NW-1")
    public_catalog.stock(part, "5")
    reservations, sales = Reservation.objects.count(), Sale.objects.count()
    before = available_totals([part.pk])

    with capture() as queries:
        _add(public_client, part, "2")
        _add(public_client, part, "4", next="cart")
        _cart(public_client)
        public_client.post(f"/cart/{part.public_id}/remove/")

    assert_no_writes(queries)
    assert (Reservation.objects.count(), Sale.objects.count()) == (reservations, sales)
    assert available_totals([part.pk]) == before


def test_cart_writes_require_csrf(public_catalog):
    from django.test import Client

    from tests.public_catalog_support import PUBLIC_HOST, public_runtime_settings

    part = public_catalog.part("Csrf part", article="CSRF-1")
    with public_runtime_settings():
        client = Client(HTTP_HOST=PUBLIC_HOST, enforce_csrf_checks=True)
        response = client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
    assert response.status_code == 403
    assert "Страница устарела" in response.content.decode()


@pytest.mark.parametrize("lines", [1, 20, 50])
def test_cart_page_query_count_is_flat(public_client, public_catalog, lines, record_property):
    for index in range(lines):
        part = public_catalog.part(f"Flat line {index}", article=f"FL-{index}")
        public_catalog.stock(part, "2")
        _add(public_client, part)

    with capture() as queries:
        response = public_client.get("/cart/")

    assert response.status_code == 200
    assert_no_writes(queries)
    record_property(f"public_cart_queries_{lines}", len(queries.captured_queries))
    assert len(queries.captured_queries) <= 14


def test_expired_cart_cookie_is_an_empty_cart(public_client, public_catalog):
    import time
    from unittest import mock

    from apps.catalog.public_settings import PUBLIC_SETTINGS

    part = public_catalog.part("Old cart part", article="OLD-1")
    public_catalog.stock(part, "1")
    _set_cookie_cart(public_client, {str(part.public_id): 1})
    assert "Old cart part" in _cart(public_client), "a fresh signed cart is read"

    too_old = time.time() - PUBLIC_SETTINGS["SESSION_COOKIE_AGE"] - 60
    with mock.patch("django.core.signing.time.time", return_value=too_old):
        _set_cookie_cart(public_client, {str(part.public_id): 1})

    assert "Корзина пуста" in _cart(public_client)
