"""Reorder consumes the canonical pricing and manual-part contracts, unchanged.

«Заказать ещё раз» must never hold pricing rules of its own. It reads the
shared public catalog card, so whatever the canonical resolver decides today
and the publication rules for manually created parts is what the preview and
the cart show, with no second implementation to keep in step.

Every test here asserts the preview against ``resolve_current_customer_price``
itself, so a future change to the resolver moves both sides together or fails
loudly here.
"""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from apps.catalog.models import PartType
from apps.catalog.public_contracts import resolve_current_customer_price
from apps.catalog.services import create_manual_part
from apps.customer_accounts import history, reorder
from apps.customer_accounts.models import CustomerAccount
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import SaleLine
from tests.customer_account_support import (
    as_account,
    bound,
    link_customer_card,
    make_customer,
    make_sale,
    public_account_runtime,
    sign_in,
)
from tests.public_catalog_support import PUBLIC_HOST

BUYER_MAX = 8900001
# The production regression the pricing release fixed: the certified price fell
# to 1 848 ₽ while stock received earlier still carried its 2 351 ₽ snapshot.
CURRENT = Decimal("1848")
LEGACY = Decimal("2351")


@pytest.fixture
def shop(public_catalog):
    """A signed-in buyer, an employee-linked card, and a lot builder."""
    def lot(part, quantity, snapshot, *, cost="10"):
        batch = Batch.objects.create(supplier=public_catalog.supplier)
        line = BatchLine.objects.create(
            batch=batch, part_type=part,
            quantity=Decimal(quantity), unit_cost_currency=Decimal(cost),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, public_catalog.user)
        line.refresh_from_db()
        created = create_stock_lot(
            line, public_catalog.location, Decimal(quantity),
            receipt_customer_price_snapshot_rub=(
                Decimal(snapshot) if snapshot is not None else None
            ),
        )
        receive_stock_lot(created, by=public_catalog.user)
        return created

    with public_account_runtime():
        token = sign_in(BUYER_MAX, name="Покупатель")
        account = CustomerAccount.objects.get()
        customer = make_customer("Покупатель-карточка")
        link_customer_card(account, customer, public_catalog.user)
        yield {
            "catalog": public_catalog, "lot": lot, "token": token,
            "account": account, "customer": customer,
            "client": as_account(Client(HTTP_HOST=PUBLIC_HOST), token),
        }


def _preview(shop, sale):
    with bound(shop["token"]):
        purchase = history.account_purchase(shop["account"], sale.number)
        assert purchase is not None
        return reorder.preview(purchase)


def _canonical(part) -> Decimal | None:
    """What the CANONICAL resolver says today — the only pricing authority."""
    part.refresh_from_db()
    price = resolve_current_customer_price(part)
    return price.price_rub if price.status == "known" else None


def _cart_amounts(client) -> set[str]:
    import re

    body = client.get(reverse("public_catalog_cart")).content.decode()
    return {
        re.sub(r"[\s  ]", "", match)
        for match in re.findall(r"[\d   ]+(?=\s*₽)", body)
    }


# --- The final pricing contract ---------------------------------------------------------


@pytest.mark.django_db
def test_reorder_preview_uses_the_current_authoritative_price(shop):
    part = shop["catalog"].part("FLEXIBLE ADAPTOR", article="707002585", price=str(CURRENT))
    lot = shop["lot"](part, "5", LEGACY)
    sale = make_sale(shop["customer"], part, lot=lot, quantity="2", unit_price="1500")

    with public_account_runtime():
        assert _canonical(part) == CURRENT
        line = _preview(shop, sale)[0]
        assert line.current_price == CURRENT
        assert line.historical_unit_price == Decimal("1500")
        assert line.usable


@pytest.mark.django_db
def test_the_reorder_price_is_the_canonical_price_in_every_state(shop):
    """Certified current prices remain canonical across price changes."""
    part = shop["catalog"].part("FLEXIBLE ADAPTOR", article="707002585", price=str(CURRENT))
    lot = shop["lot"](part, "5", None)
    sale = make_sale(shop["customer"], part, lot=lot, unit_price="1000")

    with public_account_runtime():
        # No snapshot: the certified price alone.
        assert _preview(shop, sale)[0].current_price == _canonical(part) == CURRENT

        # A higher historical snapshot does not affect today's price.
        shop["lot"](part, "3", LEGACY)
        assert _preview(shop, sale)[0].current_price == _canonical(part) == CURRENT

        # The certified price moves up past the snapshot: the higher wins again.
        PartType.objects.filter(pk=part.pk).update(
            recommended_price=Decimal("3000"), certified_price_rub=Decimal("3000")
        )
        assert _preview(shop, sale)[0].current_price == _canonical(part) == Decimal("3000")


@pytest.mark.django_db
def test_a_lower_snapshot_never_drags_the_current_price_down(shop):
    part = shop["catalog"].part("FLEXIBLE ADAPTOR", article="707002585", price=str(LEGACY))
    lot = shop["lot"](part, "5", CURRENT)  # an older, CHEAPER snapshot
    sale = make_sale(shop["customer"], part, lot=lot, unit_price="1000")

    with public_account_runtime():
        assert _preview(shop, sale)[0].current_price == _canonical(part) == LEGACY


@pytest.mark.django_db
def test_landed_cost_is_never_a_price_floor_in_the_preview(shop):
    """A costly lot with no customer snapshot must not invent a price."""
    part = shop["catalog"].part("NO PRICE", article="NP-1", price=None)
    lot = shop["lot"](part, "5", None, cost="9999")
    sale = make_sale(shop["customer"], part, lot=lot, unit_price="1000")

    with public_account_runtime():
        line = _preview(shop, sale)[0]
        assert line.current_price is None and _canonical(part) is None
        assert line.current_price != Decimal("0")


@pytest.mark.django_db
def test_the_current_price_follows_through_to_the_cart(shop):
    part = shop["catalog"].part("FLEXIBLE ADAPTOR", article="707002585", price=str(CURRENT))
    lot = shop["lot"](part, "5", LEGACY)
    sale = make_sale(shop["customer"], part, lot=lot, quantity="1", unit_price="1000")

    with public_account_runtime():
        shop["client"].post(reverse("customer_account_reorder", args=[sale.number]))
        amounts = _cart_amounts(shop["client"])
        assert "1848" in amounts, amounts
        assert "2351" not in amounts, amounts
        assert "1000" not in amounts, amounts  # never the historical one


@pytest.mark.django_db
def test_a_completed_sale_line_is_never_recomputed_by_the_floor(shop):
    """History shows what was charged; only the preview is re-decided."""
    part = shop["catalog"].part("FLEXIBLE ADAPTOR", article="707002585", price=str(CURRENT))
    lot = shop["lot"](part, "5", LEGACY)
    sale = make_sale(shop["customer"], part, lot=lot, quantity="2", unit_price="1500")

    with public_account_runtime(), bound(shop["token"]):
        purchase = history.account_purchase(shop["account"], sale.number)
        assert purchase.lines[0].unit_price == Decimal("1500")
        assert purchase.total == Decimal("3000")


# --- Manual parts -------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_manual_public_part_with_a_known_price_reorders_like_any_other(shop):
    part = create_manual_part(name="Топливный фильтр", article="MAN-FILTER-01",
                              price=Decimal("2350"))
    lot = shop["lot"](part, "4", None)
    sale = make_sale(shop["customer"], part, lot=lot, quantity="1", unit_price="2000")

    with public_account_runtime():
        line = _preview(shop, sale)[0]
        assert line.state == reorder.OK and line.usable
        assert line.current_price == _canonical(part) == Decimal("2350.00")
        assert part.price_provenance == PartType.PriceProvenance.VALID_MANUAL_EXCEPTION


@pytest.mark.django_db
def test_a_manual_public_part_without_a_price_stays_unknown_never_zero(shop):
    part = create_manual_part(name="Фильтр без цены")
    lot = shop["lot"](part, "4", None)
    sale = make_sale(shop["customer"], part, lot=lot, unit_price="900")

    with public_account_runtime():
        line = _preview(shop, sale)[0]
        assert line.current_price is None
        assert line.current_price != Decimal("0")
        assert _canonical(part) is None
        # Unknown does not make it unorderable: the customer may still ask.
        assert line.usable and line.state == reorder.OK


@pytest.mark.django_db
def test_a_manual_part_with_zero_stock_becomes_a_supply_inquiry(shop):
    part = create_manual_part(name="Манжета", article="MAN-2", price=Decimal("700"))
    lot = shop["lot"](part, "2", None)
    sale = make_sale(shop["customer"], part, lot=lot, quantity="1", unit_price="700")
    from apps.inventory.models import StockLot

    StockLot.objects.filter(pk=lot.pk).update(quantity=Decimal("0"))

    with public_account_runtime():
        line = _preview(shop, sale)[0]
        assert line.state == reorder.INQUIRY and line.usable


@pytest.mark.django_db
def test_an_intentionally_private_manual_part_cannot_be_reordered(shop):
    part = create_manual_part(name="Внутренняя заготовка", price=Decimal("100"))
    lot = shop["lot"](part, "3", None)
    sale = make_sale(shop["customer"], part, lot=lot, unit_price="100")
    PartType.objects.filter(pk=part.pk).update(is_public=False)

    with public_account_runtime():
        line = _preview(shop, sale)[0]
        assert line.state == reorder.NOT_PUBLIC
        assert not line.usable and line.current_price is None
        added = shop["client"].post(
            reverse("customer_account_reorder", args=[sale.number])
        )
        assert added.status_code == 302
        assert "Внутренняя заготовка" not in shop["client"].get(
            reverse("public_catalog_cart")
        ).content.decode()


@pytest.mark.django_db
def test_a_manual_part_that_no_longer_exists_degrades_to_unavailable(shop):
    part = create_manual_part(name="Снятая позиция", article="MAN-3", price=Decimal("500"))
    lot = shop["lot"](part, "3", None)
    sale = make_sale(shop["customer"], part, lot=lot, unit_price="500")

    with public_account_runtime(), bound(shop["token"]):
        purchase = history.account_purchase(shop["account"], sale.number)
        gone_id = PartType.objects.order_by("-pk").first().pk + 10_000
        purchase.lines[0] = history.PurchaseLine(
            part_type_id=gone_id, article="MAN-3", name="Снятая позиция",
            quantity=Decimal("1"), unit_price=Decimal("500"), total_price=Decimal("500"),
        )
        line = reorder.preview(purchase)[0]
        assert line.state == reorder.MISSING
        assert not line.usable and line.current_price is None


@pytest.mark.django_db
def test_a_manual_and_a_catalog_part_in_one_purchase_are_treated_alike(shop):
    catalog_part = shop["catalog"].part("PISTON ASSY", article="420892388", price="1000")
    catalog_lot = shop["lot"](catalog_part, "5", None)
    manual = create_manual_part(name="Ручная деталь", article="MAN-4", price=Decimal("640"))
    manual_lot = shop["lot"](manual, "5", None)
    sale = make_sale(shop["customer"], catalog_part, lot=catalog_lot,
                     quantity="1", unit_price="1000")
    SaleLine.objects.create(
        sale=sale, part_type=manual, stock_lot=manual_lot,
        batch=manual_lot.batch_line.batch, batch_line=manual_lot.batch_line,
        quantity=Decimal("1"), unit_price=Decimal("600"), total_price=Decimal("600"),
    )

    with public_account_runtime():
        lines = {line.name: line for line in _preview(shop, sale)}
        assert set(lines) == {"PISTON ASSY", "Ручная деталь"}
        assert all(line.usable and line.state == reorder.OK for line in lines.values())
        assert lines["Ручная деталь"].current_price == _canonical(manual) == Decimal("640.00")
        assert lines["PISTON ASSY"].current_price == _canonical(catalog_part)
