"""The current authoritative customer price is used everywhere.

Production regression (FLEXIBLE ADAPTOR, 707002585): the current certified
price is 1 848 ₽ while stock received by the initial count still carries an
older customer-price snapshot of 2 351 ₽. The current price source alone is
authoritative; the old snapshot remains historical evidence only.

Landed cost is never a floor, unknown stays unknown, and completed sale lines
are never recomputed.
"""

from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.actions.cart import open_cart, set_row_quantity
from apps.actions.models import PartCustomsInfo
from apps.actions.services import perform_action, stock_overview
from apps.catalog.models import PartType
from apps.catalog.public_contracts import (
    build_public_part_facts,
    resolve_current_customer_price,
    resolve_current_customer_prices,
)
from apps.core.search import search_parts
from apps.core.templatetags.number_format import money_int
from apps.customer_requests.models import CustomerRequest
from apps.customer_requests.services import RequestLineInput, create_customer_request
from apps.inventory.models import StockLot
from apps.inventory.pricing import (
    effective_part_customer_prices,
)
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import SaleLine

CURRENT = Decimal("1848")
LEGACY = Decimal("2351")
POLICY = "draft-legal-review-1"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def scene(public_catalog):
    part = public_catalog.part("FLEXIBLE ADAPTOR", article="707002585", price=str(CURRENT))

    def lot(quantity, snapshot, *, cost="10", target=None):
        target = target or part
        batch = Batch.objects.create(supplier=public_catalog.supplier)
        line = BatchLine.objects.create(
            batch=batch,
            part_type=target,
            quantity=Decimal(quantity),
            unit_cost_currency=Decimal(cost),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, public_catalog.user)
        line.refresh_from_db()
        created = create_stock_lot(
            line,
            public_catalog.location,
            Decimal(quantity),
            receipt_customer_price_snapshot_rub=(
                Decimal(snapshot) if snapshot is not None else None
            ),
        )
        receive_stock_lot(created, by=public_catalog.user)
        return created

    return public_catalog, part, lot


def _set_current(part, price):
    part.recommended_price = price
    part.certified_price_rub = price
    part.price_provenance = (
        PartType.PriceProvenance.FORMULA_CERTIFIED
        if price is not None
        else PartType.PriceProvenance.UNVERIFIED
    )
    part.save(update_fields=["recommended_price", "certified_price_rub", "price_provenance"])


def _effective(part):
    part.refresh_from_db()
    return effective_part_customer_prices([part])[part.pk]


# --- Current-price rule ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "legacy", "expected"),
    [
        (CURRENT, LEGACY, CURRENT),
        (Decimal("2600"), LEGACY, Decimal("2600")),
        (LEGACY, LEGACY, LEGACY),
        (CURRENT, None, CURRENT),
        (None, LEGACY, None),
        (None, None, None),
    ],
)
def test_current_price_matrix(current, legacy, expected):
    del legacy
    assert current == expected


def test_flexible_adaptor_uses_the_lower_current_price(scene):
    _catalog, part, lot = scene
    lot("3", LEGACY)
    lot("1", LEGACY)

    assert _effective(part) == CURRENT
    assert resolve_current_customer_price(part).price_rub == CURRENT


def test_higher_current_price_wins_over_the_legacy_price(scene):
    _catalog, part, lot = scene
    lot("1", LEGACY)
    _set_current(part, Decimal("2600"))

    assert _effective(part) == Decimal("2600")
    assert resolve_current_customer_price(part).price_rub == Decimal("2600")


def test_without_protected_stock_the_current_price_stands(scene):
    _catalog, part, lot = scene
    lot("2", None)

    assert _effective(part) == CURRENT
    assert resolve_current_customer_price(part).price_rub == CURRENT


def test_landed_cost_and_snapshots_are_not_current_price_sources(scene):
    _catalog, part, lot = scene
    lot("2", None, cost="99999")

    assert _effective(part) == CURRENT


def test_current_unknown_does_not_resurrect_legacy_price(scene):
    _catalog, part, lot = scene
    lot("1", LEGACY)
    _set_current(part, None)

    assert _effective(part) is None
    public = resolve_current_customer_price(part)
    assert public.status == "clarify"
    assert public.price_rub is None


def test_both_unknown_is_unknown_not_zero(scene, client):
    catalog, part, lot = scene
    lot("1", None)
    _set_current(part, None)

    assert _effective(part) is None
    assert resolve_current_customer_price(part).price_rub is None
    client.force_login(catalog.user)
    html = client.get(reverse("part_search"), {"q": "707002585"}).content.decode()
    assert "Цена: -" in html
    assert "Цена: 0" not in html


# --- 7: several lots ----------------------------------------------------------------------


def test_several_lots_do_not_change_the_current_price(scene, django_user_model):
    catalog, part, lot = scene
    _set_current(part, Decimal("1800"))
    older = lot("2", "2000")
    newer = lot("2", "2350")

    assert _effective(part) == Decimal("1800")

    cart = open_cart("sale", by=catalog.user)
    set_row_quantity(cart, part, catalog.location, Decimal("4"), by=catalog.user)
    prices = list(cart.lines.order_by("stock_lot_id").values_list("stock_lot_id", "unit_price"))
    assert prices == [(older.pk, Decimal("1800.00")), (newer.pk, Decimal("1800.00"))]

    # A lot that has left the warehouse no longer holds the part price.
    StockLot.objects.filter(pk=newer.pk).update(quantity=0, status=StockLot.Status.DEPLETED)
    assert _effective(part) == Decimal("1800")
    StockLot.objects.filter(pk=older.pk).update(status=StockLot.Status.WRITTEN_OFF)
    assert _effective(part) == Decimal("1800")


# --- 8, 9, 12: sale flow and immutable history -------------------------------------------


def test_quick_sell_uses_current_price_and_history_stays_frozen(scene):
    catalog, part, lot = scene
    lot("3", LEGACY)
    # A quick sale insists on a complete customs card; the price rule does not care.
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="ПЕРЕХОДНИК ГИБКИЙ",
        customs_name_ru_confirmed=True,
        application_area=PartCustomsInfo.ApplicationArea.SNOWMOBILE,
        gross_weight_kg=Decimal("0.350"),
        net_weight_kg=Decimal("0.300"),
    )

    action = perform_action(
        part=part,
        location=catalog.location,
        action_type="sale",
        quantity="1",
        customer_comment="Покупатель",
        by=catalog.user,
    )
    line = action.sale.lines.get()
    assert line.unit_price == CURRENT
    assert line.total_price == CURRENT
    frozen = SaleLine.objects.filter(pk=line.pk).values_list(
        "unit_price", "total_price", "unit_cost_rub"
    ).get()

    # The current price moves both ways; the completed line never follows it.
    _set_current(part, Decimal("3000"))
    assert _effective(part) == Decimal("3000")
    _set_current(part, Decimal("1000"))
    assert _effective(part) == Decimal("1000")
    assert SaleLine.objects.filter(pk=line.pk).values_list(
        "unit_price", "total_price", "unit_cost_rub"
    ).get() == frozen
    action.sale.refresh_from_db()
    assert action.sale.lines.get().unit_price == CURRENT


def test_sale_cart_defaults_to_the_current_price(scene):
    catalog, part, lot = scene
    lot("3", LEGACY)

    cart = open_cart("sale", by=catalog.user)
    set_row_quantity(cart, part, catalog.location, Decimal("2"), by=catalog.user)

    assert cart.lines.get().unit_price == CURRENT


# --- 10: CustomerRequest price_seen --------------------------------------------------------


def test_customer_request_price_seen_is_the_effective_price_and_stays_frozen(scene):
    _catalog, part, lot = scene
    lot("3", LEGACY)

    request, created = create_customer_request(
        customer_name="Иван Петров",
        customer_phone="+7 (912) 123-45-67",
        preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
        lines=[RequestLineInput(part_id=part.pk, quantity="1", supply_inquiry=False)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key="p" * 32,
    )
    assert created
    assert request.lines.get().price_seen == CURRENT

    _set_current(part, Decimal("3000"))
    assert request.lines.get().price_seen == CURRENT


# --- 11: PRO-STOR ---------------------------------------------------------------------------


def test_public_surfaces_show_the_current_authoritative_price(scene, public_client):
    _catalog, part, lot = scene
    lot("3", LEGACY)
    lot("1", LEGACY)
    # PRO-STOR groups rubles with no-break spaces.
    shown = "1\N{NO-BREAK SPACE}848\N{NO-BREAK SPACE}₽"
    legacy = "2\N{NO-BREAK SPACE}351"

    search = public_client.get("/search/?q=707002585").content.decode()
    detail = public_client.get(f"/parts/{part.public_id}/").content.decode()
    public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
    cart = public_client.get("/cart/").content.decode()
    form = public_client.get("/request/").content.decode()

    for page in (search, detail, cart, form):
        assert shown in page
        assert legacy not in page

    token = form.split('name="submission_key" value="', 1)[1].split('"', 1)[0]
    response = public_client.post(
        "/request/submit/",
        {
            "submission_key": token,
            "customer_name": "Иван Петров",
            "customer_phone": "9001234567",
            "preferred_messenger": "telegram",
            "comment": "",
            "consent": "1",
            "price_seen": str(CURRENT),
        },
    )
    assert response.status_code == 302
    assert CustomerRequest.objects.get().lines.get().price_seen == CURRENT


# --- Internal operator screens ----------------------------------------------------------------


def test_internal_screens_show_the_current_price(scene, client):
    catalog, part, lot = scene
    lot_329 = lot("3", LEGACY)
    client.force_login(catalog.user)
    current = money_int(CURRENT)
    legacy = money_int(LEGACY)

    search = client.get(reverse("part_search"), {"q": "707002585"}).content.decode()
    assert f"Цена: {current}" in search
    assert f"Цена: {legacy}" not in search

    detail = client.get(reverse("part_detail", args=[part.pk])).content.decode()
    assert current in detail

    lot_page = client.get(reverse("lot_detail", args=[lot_329.pk])).content.decode()
    assert f"{current} ₽" in lot_page

    assert stock_overview(part)["lookup"].client_price == CURRENT
    assert search_parts("707002585")[0].client_price == CURRENT


# --- Performance -------------------------------------------------------------------------------


def test_bulk_resolution_has_a_constant_query_count(scene):
    catalog, _part, lot = scene
    parts = []
    for index in range(12):
        extra = catalog.part(f"BULK PART {index}", article=f"BULK-{index}", price="1000")
        lot("1", "1500", target=extra)
        parts.append(extra)

    counts = []
    for size in (1, 12):
        with CaptureQueriesContext(connection) as captured:
            prices = resolve_current_customer_prices(parts[:size])
        counts.append(len(captured))
        assert {price.price_rub for price in prices.values()} == {Decimal("1000")}
    assert counts[0] == counts[1] == 0

    counts = []
    for size in (1, 12):
        with CaptureQueriesContext(connection) as captured:
            facts = build_public_part_facts([part.pk for part in parts[:size]])
        counts.append(len(captured))
        assert {fact.price.price_rub for fact in facts} == {Decimal("1000")}
    assert counts[0] == counts[1]
