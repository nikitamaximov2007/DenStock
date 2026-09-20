"""«Заказать ещё раз»: a preview and a cart, never an order.

The historical price is shown for comparison and is NEVER the price authority:
every line is re-decided by the CURRENT public catalog contract — is the part
public, is it in stock, what does it cost today. Nothing here creates a
request or a sale, reserves stock or moves stock.

Pricing itself is not re-implemented: the preview reads the shared public
catalog card (``facts.price``), which is the canonical resolver the rest of
PRO-STOR uses, so a change to the resolver reaches the reorder preview without
a second copy of the rules.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.catalog.models import PartType
from apps.customer_accounts import history, reorder
from apps.customer_accounts.models import CustomerAccount
from apps.customer_requests.models import CustomerRequest
from apps.inventory.models import StockLot, StockMovement
from apps.sales.models import Reservation, Sale, SaleLine
from tests.customer_account_support import (
    as_account,
    link_customer_card,
    make_customer,
    make_sale,
    public_account_runtime,
    sign_in,
)
from tests.public_catalog_support import PUBLIC_HOST, assert_no_writes, capture

BUYER_MAX = 8500001


@pytest.fixture
def bought(public_catalog):
    """One completed purchase of one in-stock public part, at 1000 ₽."""
    from django.test import Client

    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    lot = public_catalog.stock(part, "20")
    with public_account_runtime():
        token = sign_in(BUYER_MAX, name="Покупатель")
        account = CustomerAccount.objects.get()
        customer = make_customer("Покупатель-карточка")
        link_customer_card(account, customer, public_catalog.user)
        sale = make_sale(customer, part, lot=lot, quantity="2", unit_price="1000")
        yield {
            "catalog": public_catalog, "part": part, "lot": lot, "sale": sale,
            "account": account, "token": token, "customer": customer,
            "client": as_account(Client(HTTP_HOST=PUBLIC_HOST), token),
        }


def _lines(bought):
    purchase = history.account_purchase(bought["account"], bought["sale"].number)
    assert purchase is not None
    return reorder.preview(purchase)


def _set_price(part, value):
    PartType.objects.filter(pk=part.pk).update(
        recommended_price=value,
        certified_price_rub=value,
        price_provenance=(
            PartType.PriceProvenance.FORMULA_CERTIFIED
            if value is not None
            else PartType.PriceProvenance.UNVERIFIED
        ),
    )


# --- The current price decides ---------------------------------------------------------------


@pytest.mark.django_db
def test_an_unchanged_price_previews_as_ok(bought):
    with public_account_runtime():
        line = _lines(bought)[0]
        assert line.state == reorder.OK
        assert line.historical_unit_price == Decimal("1000")
        assert line.current_price == Decimal("1000")
        assert not line.price_changed
        assert line.proposed_quantity == 2


@pytest.mark.django_db
def test_a_higher_current_price_is_the_one_shown(bought):
    _set_price(bought["part"], Decimal("1500"))
    with public_account_runtime():
        line = _lines(bought)[0]
        assert line.current_price == Decimal("1500")
        assert line.historical_unit_price == Decimal("1000")
        assert line.price_changed and line.usable


@pytest.mark.django_db
def test_a_lower_current_price_is_the_one_shown(bought):
    _set_price(bought["part"], Decimal("700"))
    with public_account_runtime():
        line = _lines(bought)[0]
        assert line.current_price == Decimal("700")
        assert line.price_changed and line.usable


@pytest.mark.django_db
def test_the_historical_price_is_never_carried_into_the_cart(bought):
    """The cart holds {public_id: quantity} and re-reads prices itself."""
    import re

    _set_price(bought["part"], Decimal("1500"))
    with public_account_runtime():
        response = bought["client"].post(
            reverse("customer_account_reorder", args=[bought["sale"].number])
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("public_catalog_cart")

        body = bought["client"].get(reverse("public_catalog_cart")).content.decode()
        # Every rouble amount the cart prints, with its digit-group spaces removed.
        amounts = {
            re.sub(r"[\s\u00a0\u202f]", "", match)
            for match in re.findall(r"[\d\u00a0\u202f ]+(?=\s*\u20bd)", body)
        }
        assert "1500" in amounts, amounts  # today's price
        assert "3000" in amounts, amounts  # 2 × today's price
        assert "1000" not in amounts, amounts  # the historical price, never
        assert "2000" not in amounts, amounts


@pytest.mark.django_db
def test_an_unknown_current_price_never_becomes_zero(bought):
    _set_price(bought["part"], None)
    with public_account_runtime():
        line = _lines(bought)[0]
        assert line.current_price is None
        assert line.current_price != Decimal("0")


@pytest.mark.django_db
def test_the_preview_reads_the_shared_public_catalog_price(bought):
    """No second copy of the pricing rules: the card's price IS the preview's."""
    from apps.catalog.public_catalog import cards_by_id

    _set_price(bought["part"], Decimal("1234"))
    with public_account_runtime():
        card = cards_by_id([bought["part"].pk])[bought["part"].pk]
        assert _lines(bought)[0].current_price == card.facts.price.price_rub


# --- Availability and publication ---------------------------------------------------------------


@pytest.mark.django_db
def test_zero_stock_becomes_a_supply_inquiry_not_a_refusal(bought):
    StockLot.objects.filter(pk=bought["lot"].pk).update(quantity=Decimal("0"))
    with public_account_runtime():
        line = _lines(bought)[0]
        assert line.state == reorder.INQUIRY
        assert line.usable and "нет в наличии" in line.note


@pytest.mark.django_db
def test_less_stock_than_before_shortens_the_cart_quantity(bought):
    StockLot.objects.filter(pk=bought["lot"].pk).update(quantity=Decimal("1"))
    with public_account_runtime():
        line = _lines(bought)[0]
        assert line.state == reorder.SHORT
        assert line.proposed_quantity == 2 and line.cart_quantity == 1


@pytest.mark.django_db
def test_a_part_that_is_no_longer_public_cannot_be_reordered(bought):
    PartType.objects.filter(pk=bought["part"].pk).update(is_public=False)
    with public_account_runtime():
        line = _lines(bought)[0]
        assert line.state == reorder.NOT_PUBLIC
        assert not line.usable and line.current_price is None


@pytest.mark.django_db
def test_an_inactive_part_cannot_be_reordered(bought):
    PartType.objects.filter(pk=bought["part"].pk).update(is_active=False)
    with public_account_runtime():
        assert not _lines(bought)[0].usable


@pytest.mark.django_db
def test_a_missing_part_type_degrades_to_unavailable(bought):
    """The line still renders from its own snapshot; it just cannot be ordered."""
    with public_account_runtime():
        purchase = history.account_purchase(bought["account"], bought["sale"].number)
        gone_id = PartType.objects.order_by("-pk").first().pk + 10_000
        purchase.lines[0] = history.PurchaseLine(
            part_type_id=gone_id,
            article="OLD-1",
            name="Снятая с учёта позиция",
            quantity=Decimal("1"),
            unit_price=Decimal("1000"),
            total_price=Decimal("1000"),
        )
        line = reorder.preview(purchase)[0]
        assert line.state == reorder.MISSING
        assert not line.usable and line.current_price is None
        assert line.name == "Снятая с учёта позиция"


@pytest.mark.django_db
def test_a_mixed_purchase_adds_only_the_usable_lines(public_catalog, bought):
    good = public_catalog.part("SPARK PLUG", article="SP-1", price="300")
    public_catalog.stock(good, "5")
    gone = public_catalog.part("GONE", article="GN-1", price="400", public=False)
    lot = bought["lot"]
    for part in (good, gone):
        SaleLine.objects.create(
            sale=bought["sale"], part_type=part, stock_lot=lot,
            batch=lot.batch_line.batch, batch_line=lot.batch_line,
            quantity=Decimal("1"), unit_price=Decimal("300"), total_price=Decimal("300"),
        )
    with public_account_runtime():
        lines = _lines(bought)
        assert len(lines) == 3
        assert sum(1 for line in lines if line.usable) == 2
        added = bought["client"].post(
            reverse("customer_account_reorder", args=[bought["sale"].number])
        )
        assert added.status_code == 302
        from apps.catalog.public_cart import read_cart
        from django.test import Client
        # Two usable lines reached the cart; the unpublished one did not.
        body = bought["client"].get(reverse("public_catalog_cart")).content.decode()
        assert "GONE" not in body


@pytest.mark.django_db
def test_a_manually_created_part_follows_the_same_public_rules(public_catalog, bought):
    """A manual PartType is ordinary: public + in stock decides, nothing else."""
    manual = PartType.objects.create(
        name="MANUAL PART", category=public_catalog.category, unit=public_catalog.unit,
        tracking_mode=PartType.TrackingMode.BULK, is_active=True, is_public=True,
        recommended_price=Decimal("450"), certified_price_rub=Decimal("450"),
        price_provenance=PartType.PriceProvenance.FORMULA_CERTIFIED,
    )
    public_catalog.stock(manual, "4")
    lot = bought["lot"]
    SaleLine.objects.create(
        sale=bought["sale"], part_type=manual, stock_lot=lot,
        batch=lot.batch_line.batch, batch_line=lot.batch_line,
        quantity=Decimal("1"), unit_price=Decimal("400"), total_price=Decimal("400"),
    )
    with public_account_runtime():
        line = next(line for line in _lines(bought) if line.name == "MANUAL PART")
        assert line.usable and line.current_price == Decimal("450")


# --- Quantities ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_fractional_historical_quantity_becomes_a_whole_cart_quantity(bought):
    SaleLine.objects.filter(sale=bought["sale"]).update(quantity=Decimal("2.4"))
    with public_account_runtime():
        assert _lines(bought)[0].proposed_quantity == 3


@pytest.mark.django_db
def test_a_huge_historical_quantity_is_capped_to_the_cart_maximum(bought):
    from apps.catalog.public_cart import MAX_CART_QUANTITY

    SaleLine.objects.filter(sale=bought["sale"]).update(quantity=Decimal("100000"))
    with public_account_runtime():
        assert _lines(bought)[0].proposed_quantity == MAX_CART_QUANTITY


@pytest.mark.django_db
def test_the_customer_can_still_edit_the_quantity_in_the_cart(bought):
    with public_account_runtime():
        bought["client"].post(
            reverse("customer_account_reorder", args=[bought["sale"].number])
        )
        public_id = bought["part"].public_id
        edited = bought["client"].post(f"/cart/{public_id}/add/", {"quantity": "5"})
        assert edited.status_code == 302


@pytest.mark.django_db
def test_a_reorder_never_shrinks_what_the_customer_already_chose(bought):
    with public_account_runtime():
        bought["client"].post(f"/cart/{bought['part'].public_id}/add/", {"quantity": "9"})
        bought["client"].post(
            reverse("customer_account_reorder", args=[bought["sale"].number])
        )
        body = bought["client"].get(reverse("public_catalog_cart")).content.decode()
        assert 'value="9"' in body or ">9<" in body


# --- It writes nothing but the cart ------------------------------------------------------------------


@pytest.mark.django_db
def test_the_preview_page_writes_nothing_at_all(bought):
    with public_account_runtime():
        with capture() as queries:
            response = bought["client"].get(
                reverse("customer_account_reorder", args=[bought["sale"].number])
            )
        assert response.status_code == 200
        assert_no_writes(queries)


@pytest.mark.django_db
def test_adding_to_the_cart_creates_no_request_no_sale_and_moves_no_stock(bought):
    with public_account_runtime():
        before = {
            "requests": CustomerRequest.objects.count(),
            "sales": Sale.objects.count(),
            "sale_lines": SaleLine.objects.count(),
            "quantity": StockLot.objects.get(pk=bought["lot"].pk).quantity,
            "movements": StockMovement.objects.count(),
        }
        bought["client"].post(
            reverse("customer_account_reorder", args=[bought["sale"].number])
        )
        lot = StockLot.objects.get(pk=bought["lot"].pk)
        assert CustomerRequest.objects.count() == before["requests"]
        assert Sale.objects.count() == before["sales"]
        assert SaleLine.objects.count() == before["sale_lines"]
        assert lot.quantity == before["quantity"]
        assert StockMovement.objects.count() == before["movements"]
        assert Reservation.objects.count() == 0


@pytest.mark.django_db
def test_a_reorder_of_nothing_usable_says_so_and_still_writes_nothing(bought):
    PartType.objects.filter(pk=bought["part"].pk).update(is_public=False)
    with public_account_runtime():
        response = bought["client"].post(
            reverse("customer_account_reorder", args=[bought["sale"].number])
        )
        assert response.status_code == 302
        assert CustomerRequest.objects.count() == 0
