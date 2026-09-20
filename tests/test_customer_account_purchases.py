"""Purchase history: Sale/SaleLine only, historical prices, nothing internal.

«Покупки» is the DenisStock sale document, not the customer's request. What
the customer sees is the immutable ``SaleLine`` price they were charged at the
time. Cost, landed cost, margin, supplier and employee never leave the
internal side: the PostgreSQL views do not select those columns, and on SQLite
``history`` reads the same short list.
"""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from apps.customer_accounts import history
from apps.customer_accounts.models import CustomerAccount
from apps.sales.models import Sale
from tests.customer_account_support import (
    INTERNAL_COST,
    INTERNAL_PROFIT,
    INTERNAL_UNIT_COST,
    as_account,
    bound,
    link_customer_card,
    make_customer,
    make_sale,
    public_account_runtime,
    sign_in,
)
from tests.public_catalog_support import PUBLIC_HOST

BUYER_MAX = 8400001
# Every internal money value the support builder puts on a sale and its line,
# in both the raw and the thousands-separated form the price filter would use.
INTERNAL_NUMBERS = [
    str(value)
    for raw in (INTERNAL_COST, INTERNAL_PROFIT, INTERNAL_UNIT_COST)
    for value in (raw, f"{int(raw) // 1000}\u00a0{int(raw) % 1000:03d}", f"{int(raw):,}")
]


@pytest.fixture
def buyer(public_catalog):
    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    lot = public_catalog.stock(part, "20")
    with public_account_runtime():
        token = sign_in(BUYER_MAX, name="Покупатель")
        account = CustomerAccount.objects.get()
        customer = make_customer("Покупатель-карточка")
        link_customer_card(account, customer, public_catalog.user)
        yield {
            "part": part, "lot": lot, "account": account, "customer": customer,
            "token": token, "client": as_account(Client(HTTP_HOST=PUBLIC_HOST), token),
            "catalog": public_catalog,
        }


# --- What counts as a purchase ----------------------------------------------------------------


@pytest.mark.django_db
def test_a_completed_sale_appears_with_its_historical_line_price(buyer):
    sale = make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"],
                     quantity="3", unit_price="1200")
    with public_account_runtime(), bound(buyer["token"]):
        purchases = history.account_purchases(buyer["account"])
        assert len(purchases) == 1
        purchase = purchases[0]
        assert purchase.number == sale.number
        assert purchase.lines[0].unit_price == Decimal("1200")
        assert purchase.lines[0].quantity == Decimal("3")
        assert purchase.total == Decimal("3600")


@pytest.mark.django_db
def test_a_draft_sale_is_not_a_purchase(buyer):
    make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"], status=Sale.Status.DRAFT)
    with public_account_runtime(), bound(buyer["token"]):
        assert history.account_purchases(buyer["account"]) == []


@pytest.mark.django_db
def test_a_canceled_sale_is_not_a_purchase(buyer):
    """Canceled follows the existing DenisStock rule: only 'completed' shows."""
    sale = make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"])
    with public_account_runtime(), bound(buyer["token"]):
        assert len(history.account_purchases(buyer["account"])) == 1
    Sale.objects.filter(pk=sale.pk).update(status=Sale.Status.CANCELED)
    with public_account_runtime(), bound(buyer["token"]):
        assert history.account_purchases(buyer["account"]) == []


@pytest.mark.django_db
def test_the_historical_price_never_moves_when_the_current_price_changes(buyer):
    from apps.catalog.models import PartType

    make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"], unit_price="1000")
    PartType.objects.filter(pk=buyer["part"].pk).update(
        recommended_price=Decimal("9999"), certified_price_rub=Decimal("9999")
    )
    with public_account_runtime(), bound(buyer["token"]):
        purchase = history.account_purchases(buyer["account"])[0]
        assert purchase.lines[0].unit_price == Decimal("1000")


@pytest.mark.django_db
def test_purchases_are_newest_first(buyer):
    older = make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"], unit_price="100")
    newer = make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"], unit_price="200")
    Sale.objects.filter(pk=older.pk).update(sold_at=newer.sold_at.replace(year=2020))
    with public_account_runtime(), bound(buyer["token"]):
        numbers = [p.number for p in history.account_purchases(buyer["account"])]
        assert numbers == [newer.number, older.number]


@pytest.mark.django_db
def test_a_purchase_of_a_part_that_no_longer_exists_still_renders(buyer):
    from apps.catalog.models import PartType

    sale = make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"])
    PartType.objects.filter(pk=buyer["part"].pk).update(is_public=False, is_active=False)
    with public_account_runtime(), bound(buyer["token"]):
        purchase = history.account_purchase(buyer["account"], sale.number)
        assert purchase is not None and purchase.lines[0].name


# --- Nothing internal ever leaves -------------------------------------------------------------


@pytest.mark.django_db
def test_the_purchase_page_never_shows_cost_margin_or_landed_cost(buyer):
    sale = make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"],
                     quantity="2", unit_price="999")
    with public_account_runtime():
        body = buyer["client"].get(
            reverse("customer_account_purchase", args=[sale.number])
        ).content.decode()
        assert "999" in body  # the customer price they paid IS shown
        for internal in INTERNAL_NUMBERS:
            assert internal not in body, internal
        for word in ["себестоим", "Себестоим", "прибыл", "Прибыл", "маржа", "Маржа",
                     "поставщик", "Поставщик"]:
            assert word not in body, word


@pytest.mark.django_db
def test_the_purchase_list_never_shows_internal_numbers(buyer):
    make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"])
    with public_account_runtime():
        body = buyer["client"].get(
            reverse("customer_account_purchases")
        ).content.decode()
        for internal in INTERNAL_NUMBERS:
            assert internal not in body, internal


@pytest.mark.django_db
def test_the_purchase_dataclass_carries_no_internal_field(buyer):
    """A template cannot leak what the dataclass never received."""
    make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"])
    with public_account_runtime(), bound(buyer["token"]):
        line = history.account_purchases(buyer["account"])[0].lines[0]
        fields = set(vars(line))
        assert fields == {
            "part_type_id", "article", "name", "quantity", "unit_price", "total_price"
        }
        for forbidden in ["unit_cost_rub", "total_cost_rub", "profit_rub", "batch",
                          "batch_line", "supplier", "sold_by"]:
            assert not hasattr(line, forbidden), forbidden


@pytest.mark.django_db
def test_the_purchase_header_carries_no_revenue_cost_or_profit_totals(buyer):
    make_sale(buyer["customer"], buyer["part"], lot=buyer["lot"])
    with public_account_runtime(), bound(buyer["token"]):
        purchase = history.account_purchases(buyer["account"])[0]
        assert set(vars(purchase)) == {"id", "number", "sold_at", "lines"}
        for forbidden in ["cost_total", "profit_total", "revenue_total", "sold_by",
                          "customer_phone"]:
            assert not hasattr(purchase, forbidden), forbidden


# --- Requests are not purchases ---------------------------------------------------------------


@pytest.mark.django_db
def test_the_empty_purchase_list_renders_without_inventing_data(buyer):
    with public_account_runtime():
        response = buyer["client"].get(reverse("customer_account_purchases"))
        assert response.status_code == 200
        with bound(buyer["token"]):
            assert history.account_purchases(buyer["account"]) == []
