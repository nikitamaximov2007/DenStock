"""Price and availability on public pages follow the canonical sources live.

No formula is duplicated: the page shows ``PartType.recommended_price`` as
refreshed by the existing pricing pipeline, and ``available_totals`` as
computed by the canonical stock and reservation read model.
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.services import update_current_price_settings
from apps.sales.models import Reservation, ReservationLine
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    cancel_reservation,
    create_reservation,
    expire_reservations,
)
from tests.customs_support import remember_customs


def _offer(client, part) -> tuple[str, str]:
    body = client.get(f"/parts/{part.public_id}/").content.decode()
    price = re.search(r'<p class="offer__price">\s*(.*?)\s*<', body, re.S).group(1)
    stock = re.search(r'<p class="offer__stock[^"]*">\s*(.*?)\s*</p>', body, re.S).group(1)
    return price, " ".join(stock.split())


def test_canonical_price_refresh_reaches_the_public_page(public_client, public_catalog):
    source = BrpCatalogPart.objects.create(
        material_no="PUBLIC-LIVE-001",
        part_desc="Live price source",
        retail_price_usd=Decimal("100"),
        wholesale_price_usd=Decimal("10"),
    )
    part = promote_to_warehouse(source, by=public_catalog.user)
    first, _ = _offer(public_client, part)

    update_current_price_settings(
        current_usd_rate=Decimal("100"),
        brp_markup_percent=Decimal("50"),
        polaris_markup_percent=Decimal("40"),
        by=public_catalog.user,
    )
    part.refresh_from_db()

    expected = f"{part.recommended_price:,.0f}".replace(",", "\u00a0") + "\u00a0₽"
    assert first != expected
    assert _offer(public_client, part)[0] == expected


def test_receipt_sale_and_reservations_move_public_availability(public_client, public_catalog):
    part = public_catalog.part("Live stock part", article="LSP-1")
    assert _offer(public_client, part)[1] == "Сейчас нет на складе"

    lot = public_catalog.stock(part, "5")
    assert _offer(public_client, part)[1] == "В наличии: 5 шт"

    from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale

    remember_customs(part)
    sale = create_sale(customer_name="Counter buyer", by=public_catalog.user)
    add_stock_lot_to_sale(
        sale, lot, Decimal("1"), unit_price=Decimal("100"), by=public_catalog.user
    )
    complete_sale(sale, by=public_catalog.user)
    assert _offer(public_client, part)[1] == "В наличии: 4 шт"

    held = create_reservation(customer_name="Held", by=public_catalog.user)
    add_stock_lot_to_reservation(held, lot, Decimal("3"), by=public_catalog.user)
    activate_reservation(held, by=public_catalog.user)
    assert _offer(public_client, part)[1] == "В наличии: 1 шт"

    cancel_reservation(held, by=public_catalog.user)
    assert _offer(public_client, part)[1] == "В наличии: 4 шт"


def test_reservation_expiry_restores_availability(public_client, public_catalog):
    part = public_catalog.part("Expiring part", article="EXP-1")
    lot = public_catalog.stock(part, "2")
    reservation = create_reservation(
        customer_name="Soon expired",
        expires_at=timezone.now() + timedelta(hours=1),
        by=public_catalog.user,
    )
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=public_catalog.user)
    activate_reservation(reservation, by=public_catalog.user)
    assert _offer(public_client, part)[1] == "Сейчас нет на складе"

    Reservation.objects.filter(pk=reservation.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    # An expired hold stops counting at once, before any job normalizes it.
    assert _offer(public_client, part)[1] == "В наличии: 2 шт"
    expire_reservations(by=public_catalog.user)
    assert _offer(public_client, part)[1] == "В наличии: 2 шт"
    assert ReservationLine.objects.filter(reservation=reservation).exists()


def test_zero_stock_never_hides_the_page(public_client, public_catalog):
    part = public_catalog.part("Sold out part", article="SOLD-1")
    lot = public_catalog.stock(part, "1")
    from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale

    remember_customs(part)
    sale = create_sale(customer_name="Last buyer", by=public_catalog.user)
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("10"), by=public_catalog.user)
    complete_sale(sale, by=public_catalog.user)

    response = public_client.get(f"/parts/{part.public_id}/")
    assert response.status_code == 200
    assert "Узнать о поставке" in response.content.decode()
    search = public_client.get("/search/", {"q": "SOLD-1"}).content.decode()
    assert str(part.public_id) in search
