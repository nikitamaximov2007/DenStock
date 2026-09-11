"""Sending the public cart as a customer request.

The request is the public runtime's only write. It must name only public
parts, snapshot the current price server-side, send zero-stock lines as
supply inquiries, stay idempotent per form, respect the global write freeze,
and never reserve, sell or move stock.
"""

import re
from decimal import Decimal

import pytest
from django.core.cache import cache

from apps.catalog.public_requests import SUBMISSION_SESSION_KEY
from apps.customer_requests.models import CustomerRequest
from apps.inventory.availability import available_totals
from apps.inventory.models import StockBalance, StockMovement
from apps.operations.models import DeploymentState
from apps.sales.models import Reservation, Sale

TOKEN_RE = re.compile(r'name="submission_key" value="([^"]+)"')


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()
    yield
    cache.clear()


def _add(client, part, quantity="1"):
    response = client.post(f"/cart/{part.public_id}/add/", {"quantity": quantity})
    assert response.status_code == 302
    return response


def _open_form(client):
    response = client.get("/request/")
    assert response.status_code == 200, response.status_code
    return TOKEN_RE.search(response.content.decode()).group(1)


def _form(token, **extra):
    return {
        "submission_key": token,
        "customer_name": "Иван Петров",
        "customer_phone": "+7 (912) 123-45-67",
        "preferred_messenger": "telegram",
        "comment": "Нужна деталь.",
        "consent": "1",
        **extra,
    }


def _submit(client, token, **extra):
    return client.post("/request/submit/", _form(token, **extra))


def _stock_state(part):
    return {
        "balances": StockBalance.objects.count(),
        "movements": StockMovement.objects.count(),
        "reservations": Reservation.objects.count(),
        "sales": Sale.objects.count(),
        "available": available_totals([part.pk]),
    }


def test_cart_becomes_one_request_with_server_prices(public_client, public_catalog):
    piston = public_catalog.part("PISTON ASSY", article="420892388", price="15000")
    gasket = public_catalog.part("GASKET", article="GS-1", price=None)
    public_catalog.stock(piston, "3")
    public_catalog.stock(gasket, "5")
    before = _stock_state(piston)
    _add(public_client, piston, "2")
    _add(public_client, gasket, "1")
    token = _open_form(public_client)
    piston.recommended_price = Decimal("16500.00")
    piston.save(update_fields=["recommended_price"])

    response = _submit(
        public_client,
        token,
        price="1",
        price_seen="1",
        part_id="999999",
        status="completed",
        privacy_policy_version="forged",
    )

    request = CustomerRequest.objects.get()
    assert response.status_code == 302
    assert response["Location"] == f"/request/success/{request.public_id}/"
    assert request.status == CustomerRequest.Status.NEW
    assert request.privacy_policy_version == "draft-legal-review-1"
    lines = {line.part_type_id: line for line in request.lines.all()}
    assert set(lines) == {piston.pk, gasket.pk}
    assert lines[piston.pk].price_seen == Decimal("16500.00"), "price at submission"
    assert lines[piston.pk].quantity_requested == Decimal("2")
    assert lines[piston.pk].article == "420892388"
    assert lines[gasket.pk].price_seen is None, "unknown price stays unknown"
    assert not any(line.is_supply_inquiry for line in lines.values())
    assert _stock_state(piston) == before, "a request never reserves or moves stock"

    success = public_client.get(response["Location"])
    body = success.content.decode()
    assert success.status_code == 200
    assert request.reference in body and str(request.public_id) not in body
    assert "Корзина пуста" in public_client.get("/cart/").content.decode()


def test_zero_stock_lines_are_sent_as_supply_inquiries(public_client, public_catalog):
    missing = public_catalog.part("IMPELLER", article="IMP-1", price="9000")
    before = _stock_state(missing)
    public_client.post(f"/cart/{missing.public_id}/add/", {"quantity": "4"})
    token = _open_form(public_client)

    first = _submit(public_client, token, preferred_messenger="max")
    retry = _submit(public_client, token, preferred_messenger="max", comment="подменён")

    request = CustomerRequest.objects.get()
    line = request.lines.get()
    assert first.status_code == retry.status_code == 302
    assert first["Location"] == retry["Location"], "a retry lands on the same request"
    assert line.is_supply_inquiry is True and line.quantity_requested == Decimal("4")
    assert request.comment == "Нужна деталь.", "the retry changed nothing"
    assert request.preferred_messenger == "max"
    assert _stock_state(missing) == before


def test_more_than_available_blocks_sending_until_fixed(public_client, public_catalog):
    part = public_catalog.part("STARTER", article="ST-1", price="12000")
    public_catalog.stock(part, "3")
    _add(public_client, part, "3")
    token = _open_form(public_client)
    # Someone else bought one meanwhile: the line is now short.
    lot = part.stock_lots.get()
    lot.quantity = Decimal("2")
    lot.save(update_fields=["quantity"])

    response = _submit(public_client, token)
    assert response.status_code == 302 and response["Location"] == "/cart/"
    assert CustomerRequest.objects.count() == 0
    cart = public_client.get("/cart/").content.decode()
    assert "меньше, чем выбрано" in cart
    assert "Отправить заявку</a>" not in cart
    assert public_client.get("/request/")["Location"] == "/cart/"


def test_the_service_rechecks_availability_at_the_insert(
    public_client, public_catalog, monkeypatch
):
    part = public_catalog.part("STARTER", article="ST-1", price="12000")
    public_catalog.stock(part, "3")
    _add(public_client, part, "3")
    token = _open_form(public_client)
    monkeypatch.setattr(
        "apps.customer_requests.services.available_totals", lambda ids: {part.pk: Decimal("2")}
    )

    rejected = _submit(public_client, token)
    assert rejected.status_code == 400
    assert "ST-1: Сейчас доступно 2." in rejected.content.decode()
    assert CustomerRequest.objects.count() == 0


def test_consent_and_honeypot_are_required(public_client, public_catalog):
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "1")
    _add(public_client, part)
    token = _open_form(public_client)

    no_consent = _submit(public_client, token, consent="")
    bot = _submit(public_client, token, website="http://spam.example")

    assert no_consent.status_code == bot.status_code == 400
    assert "Подтвердите согласие" in no_consent.content.decode()
    assert 'value="Иван Петров"' in no_consent.content.decode(), "entered values are kept"
    assert CustomerRequest.objects.count() == 0
    assert _submit(public_client, token).status_code == 302


def test_forged_or_stale_tokens_never_create_a_request(public_client, public_catalog):
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "5")
    _add(public_client, part, "1")

    forged = _submit(public_client, "x" * 43)
    assert forged.status_code == 302 and forged["Location"] == "/request/"

    token = _open_form(public_client)
    _add(public_client, part, "2")
    changed = _submit(public_client, token)
    assert changed.status_code == 409
    assert "Корзина изменилась" in changed.content.decode()
    assert CustomerRequest.objects.count() == 0

    fresh = TOKEN_RE.search(changed.content.decode()).group(1)
    assert fresh != token
    assert _submit(public_client, fresh).status_code == 302
    assert CustomerRequest.objects.get().lines.get().quantity_requested == Decimal("2")


def test_a_new_cart_after_success_is_a_new_request(public_client, public_catalog):
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "5")
    _add(public_client, part, "1")
    first_token = _open_form(public_client)
    assert _submit(public_client, first_token).status_code == 302

    _add(public_client, part, "1")
    second_token = _open_form(public_client)
    assert second_token != first_token
    assert _submit(public_client, second_token).status_code == 302
    assert CustomerRequest.objects.count() == 2


def test_hidden_parts_cannot_be_requested(public_client, public_catalog):
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "5")
    _add(public_client, part, "1")
    token = _open_form(public_client)
    part.is_public = False
    part.save(update_fields=["is_public"])

    response = _submit(public_client, token)
    assert response.status_code == 302 and response["Location"] == "/cart/"
    assert CustomerRequest.objects.count() == 0


def test_the_service_refuses_a_hidden_part_for_a_public_request(public_catalog):
    from apps.customer_requests.services import (
        CustomerRequestError,
        RequestLineInput,
        create_customer_request,
    )

    hidden = public_catalog.part("SEAL", article="SE-1", public=False)
    with pytest.raises(CustomerRequestError, match="недоступны"):
        create_customer_request(
            customer_name="Иван",
            customer_phone="+79121234567",
            preferred_messenger="telegram",
            lines=[RequestLineInput(hidden.pk, 1, supply_inquiry=True)],
            privacy_policy_version="v1",
            personal_data_consent_version="v1",
            submission_key="k" * 32,
        )


def test_the_success_page_belongs_to_the_sending_browser(
    public_client, public_catalog, client
):
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "5")
    _add(public_client, part, "1")
    location = _submit(public_client, _open_form(public_client))["Location"]

    from tests.public_catalog_support import PUBLIC_HOST, public_runtime_settings

    with public_runtime_settings():
        stranger = client.get(location, HTTP_HOST=PUBLIC_HOST)
    assert stranger.status_code == 404
    assert public_client.get(location).status_code == 200
    unknown = "/request/success/00000000-0000-0000-0000-000000000000/"
    assert public_client.get(unknown).status_code == 404


def test_rate_limit_per_client_address(public_client, public_catalog, settings):
    settings.PUBLIC_REQUEST_RATE_LIMIT = 2
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "50")
    for _ in range(2):
        _add(public_client, part, "1")
        assert _submit(public_client, _open_form(public_client)).status_code == 302

    _add(public_client, part, "1")
    limited = _submit(public_client, _open_form(public_client))
    assert limited.status_code == 429
    assert "Слишком много заявок" in limited.content.decode()
    assert CustomerRequest.objects.count() == 2

    _add(public_client, part, "1")
    other_address = public_client.post(
        "/request/submit/",
        _form(_open_form(public_client)),
        HTTP_X_FORWARDED_FOR="203.0.113.7",
    )
    assert other_address.status_code == 302


def test_public_requests_obey_the_global_write_freeze(public_client, public_catalog, settings):
    """In the real public runtime the write guard is active, unlike in tests."""
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "5")
    _add(public_client, part, "1")
    token = _open_form(public_client)
    state = DeploymentState.get_solo()
    generation = state.business_generation
    state.write_state = DeploymentState.WriteState.MAINTENANCE
    state.save(update_fields=["write_state", "updated_at"])
    settings.DENSTOCK_MODE = "public-catalog"
    try:
        frozen = _submit(public_client, token)
        assert frozen.status_code == 503
        assert "Приём заявок временно приостановлен" in frozen.content.decode()
        assert CustomerRequest.objects.count() == 0

        state.write_state = DeploymentState.WriteState.NORMAL
        state.save(update_fields=["write_state", "updated_at"])
        accepted = _submit(public_client, token)
    finally:
        settings.DENSTOCK_MODE = "test"
    assert accepted.status_code == 302
    assert CustomerRequest.objects.count() == 1
    state.refresh_from_db()
    assert state.business_generation > generation, "request writes are fingerprinted"


@pytest.mark.parametrize(
    ("state", "allowed"),
    [
        (DeploymentState.WriteState.NORMAL, True),
        (DeploymentState.WriteState.MAINTENANCE, False),
        (DeploymentState.WriteState.EMERGENCY_ACTIVE, False),
        (DeploymentState.WriteState.EMERGENCY_FROZEN, False),
    ],
)
def test_catalog_web_writes_only_while_the_database_is_in_normal_work(settings, state, allowed):
    from apps.operations.write_guard import _state_allows_business_write

    settings.DENSTOCK_MODE = "public-catalog"
    assert _state_allows_business_write(state) is allowed


def test_the_cart_leads_to_the_form_and_the_form_is_accessible(public_client, public_catalog):
    part = public_catalog.part("SEAL", article="SE-1", price="700")
    missing = public_catalog.part("IMPELLER", article="IMP-1")
    public_catalog.stock(part, "5")
    _add(public_client, part, "2")
    _add(public_client, missing, "1")

    cart = public_client.get("/cart/").content.decode()
    assert '<a class="btn btn--large btn--block"\n             href="/request/">' in cart

    form = public_client.get("/request/")
    body = form.content.decode()
    assert form["Cache-Control"].startswith(("max-age=0", "no-cache", "private"))
    assert '<meta name="robots" content="noindex, nofollow">' in body
    for field in ("customer_name", "customer_phone", "comment"):
        assert f'<label class="field__label" for="{field}">' in body
    assert "<legend" in body and 'type="tel"' in body and 'autocomplete="tel"' in body
    assert "Запрос о поставке, 1 шт" in body
    assert "1 400 ₽" in body
    assert 'class="hp" aria-hidden="true"' in body and 'tabindex="-1"' in body
    assert "юридической проверки" not in body
    assert "—" not in body


def test_an_empty_cart_has_no_request_form(public_client, public_catalog):
    response = public_client.get("/request/")
    assert response.status_code == 302 and response["Location"] == "/cart/"
    assert public_client.get("/request/submit/").status_code == 405


def test_the_submission_token_is_stored_only_in_the_signed_cookie(
    public_client, public_catalog
):
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "5")
    _add(public_client, part, "1")
    token = _open_form(public_client)
    from django.contrib.sessions.backends.signed_cookies import SessionStore

    stored = SessionStore(public_client.cookies["prostor_cart"].value)[SUBMISSION_SESSION_KEY]
    assert stored["token"] == token and stored["request"] == ""
    _submit(public_client, token)
    request = CustomerRequest.objects.get()
    assert request.submission_key_hash != token and len(request.submission_key_hash) == 64


@pytest.mark.parametrize("lines", [1, 20, 50])
def test_request_form_and_submit_query_counts_are_flat(
    public_client, public_catalog, lines, record_property
):
    from tests.public_catalog_support import assert_no_writes, capture

    for index in range(lines):
        part = public_catalog.part(f"Flat request line {index}", article=f"FR-{index}")
        if index % 2:
            public_catalog.stock(part, "2")
        _add(public_client, part)

    with capture() as form_queries:
        token = _open_form(public_client)
    with capture() as submit_queries:
        response = _submit(public_client, token)

    assert response.status_code == 302
    assert CustomerRequest.objects.get().lines.count() == lines
    assert_no_writes(form_queries)
    writes = [
        query["sql"]
        for query in submit_queries.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert len(writes) == 2, "one request row and one bulk insert of its lines"
    record_property(f"public_request_form_queries_{lines}", len(form_queries.captured_queries))
    record_property(f"public_request_submit_queries_{lines}", len(submit_queries.captured_queries))
    assert len(form_queries.captured_queries) <= 14
    # Rebuilds the cart view, then the service re-reads parts and stock once.
    assert len(submit_queries.captured_queries) <= 30


@pytest.mark.parametrize(
    ("extra", "field_marker"),
    [
        ({"customer_phone": "позвоните мне"}, 'id="customer_phone"'),
        ({"customer_name": "   "}, 'id="customer_name"'),
        ({"comment": "x" * 2001}, 'id="comment"'),
        ({"consent": ""}, 'name="consent"'),
    ],
)
def test_a_refused_field_is_marked_and_points_at_the_message(
    public_client, public_catalog, extra, field_marker
):
    part = public_catalog.part("SEAL", article="SE-1")
    public_catalog.stock(part, "1")
    _add(public_client, part)

    response = _submit(public_client, _open_form(public_client), **extra)

    body = response.content.decode()
    assert response.status_code == 400
    assert '<p id="request-error" class="notice notice--error" role="alert">' in body
    tag = body[body.index(field_marker) :]
    tag = tag[: tag.index(">")]
    assert 'aria-invalid="true"' in tag and 'aria-describedby="request-error"' in tag
    assert body.count('aria-invalid="true"') == 1, "only the field at fault is marked"
    assert CustomerRequest.objects.count() == 0


def test_service_errors_name_their_form_field(public_catalog):
    from apps.customer_requests.services import (
        CustomerRequestError,
        RequestLineInput,
        create_customer_request,
    )

    part = public_catalog.part("SEAL", article="SE-1")
    base = {
        "customer_name": "Иван",
        "customer_phone": "+79121234567",
        "preferred_messenger": "telegram",
        "lines": [RequestLineInput(part.pk, 1, supply_inquiry=True)],
        "privacy_policy_version": "v1",
        "personal_data_consent_version": "v1",
        "submission_key": "k" * 32,
    }
    for override, field in (
        ({"customer_name": ""}, "customer_name"),
        ({"customer_phone": "abc"}, "customer_phone"),
        ({"preferred_messenger": "sms"}, "preferred_messenger"),
        ({"comment": "x" * 2001}, "comment"),
    ):
        with pytest.raises(CustomerRequestError) as refused:
            create_customer_request(**{**base, **override})
        assert refused.value.field == field
