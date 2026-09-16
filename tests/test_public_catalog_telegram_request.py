"""The public request flow with Telegram chosen: request first, Telegram second."""

import hashlib
import re
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.cache import cache
from django.db import IntegrityError
from django.urls import reverse

from apps.customer_requests.messengers import consume_telegram_start
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestMessengerLinkToken,
    TelegramConversation,
    TelegramOutboxEvent,
)
from apps.inventory.models import StockBalance, StockMovement
from apps.receipts.models import Receipt
from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, Sale

TOKEN_RE = re.compile(r'name="submission_key" value="([^"]+)"')
LINK_RE = re.compile(r'href="(https://t\.me/[^"]+)"')


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()
    yield
    cache.clear()


def _prepare(client, catalog):
    part = catalog.part("Ремень вариатора", article="417300571")
    catalog.stock(part, "3")
    assert client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"}).status_code == 302
    return part


def _send(client, catalog, *, messenger="telegram", comment="Когда можно забрать?", part=None):
    part = part or _prepare(client, catalog)
    token = TOKEN_RE.search(client.get("/request/").content.decode()).group(1)
    response = client.post(
        "/request/submit/",
        {
            "submission_key": token,
            "customer_name": "Ольга",
            "customer_phone": "+7 (912) 555-44-33",
            "preferred_messenger": messenger,
            "comment": comment,
            "consent": "1",
        },
    )
    assert response.status_code == 302, response.status_code
    return response, token, part


def _business():
    return (
        StockMovement.objects.count(),
        StockBalance.objects.count(),
        Reservation.objects.count(),
        Sale.objects.count(),
        RepairOrder.objects.count(),
        Receipt.objects.count(),
    )


def test_telegram_request_exists_first_and_success_page_offers_the_bot(
    public_client, public_catalog, settings
):
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    part = _prepare(public_client, public_catalog)  # the stock receipt happens here
    before = _business()

    response, _token, part = _send(public_client, public_catalog, part=part)

    request = CustomerRequest.objects.get()
    assert request.preferred_messenger == "telegram"
    line = request.lines.get()
    assert line.article == "417300571"
    assert line.price_seen == part.recommended_price  # server price, never the browser
    assert _business() == before
    assert TelegramConversation.objects.get(request=request).status == "awaiting_link"
    event = TelegramOutboxEvent.objects.get()
    assert (event.kind, event.status) == ("new_request", "pending")

    page = public_client.get(response["Location"]).content.decode()
    assert request.reference in page
    assert "Продолжить в Telegram" in page
    assert LINK_RE.search(page) is None
    continue_response = public_client.post(
        reverse("public_catalog_telegram_continue", args=[request.public_id])
    )
    assert continue_response.status_code == 303
    link = continue_response["Location"]
    start = parse_qs(urlparse(link).query)["start"][0]
    assert link.startswith("https://t.me/ProStorTestBot?start=")
    assert start not in {str(request.pk), request.reference, str(request.public_id)}
    assert start not in page
    assert "912" not in link
    stored = CustomerRequestMessengerLinkToken.objects.get()
    assert stored.token_hash == hashlib.sha256(start.encode()).hexdigest()
    assert start not in stored.token_hash

    # The link from the page is the one that works: it binds the chat.
    consume_telegram_start(token=start, chat_id=424242, user_id=424242)
    assert TelegramConversation.objects.get().customer_chat_id == 424242


def test_success_page_is_graceful_when_the_bot_is_not_configured(
    public_client, public_catalog, settings
):
    settings.TELEGRAM_BOT_USERNAME = ""

    response, _token, _part = _send(public_client, public_catalog)

    page = public_client.get(response["Location"]).content.decode()
    assert CustomerRequest.objects.count() == 1
    assert "Продолжить в Telegram" not in page
    assert "Telegram сейчас недоступен" in page
    assert "Заявка отправлена" in page


def test_misconfigured_link_lifetime_never_costs_the_request(
    public_client, public_catalog, settings
):
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    settings.TELEGRAM_REQUEST_LINK_TTL_SECONDS = 1

    response, _token, _part = _send(public_client, public_catalog)

    assert CustomerRequest.objects.count() == 1
    assert not CustomerRequestMessengerLinkToken.objects.exists()
    assert TelegramOutboxEvent.objects.count() == 1
    request = CustomerRequest.objects.get()
    continued = public_client.post(
        reverse("public_catalog_telegram_continue", args=[request.public_id])
    )
    assert continued["Location"] == response["Location"]
    assert "Telegram сейчас недоступен" in public_client.get(response["Location"]).content.decode()


@pytest.mark.parametrize(
    "target",
    [
        "apps.customer_requests.telegram_service.TelegramConversation.objects.create",
        "apps.customer_requests.telegram_service.TelegramOutboxEvent.objects.create",
        "apps.customer_requests.messengers.CustomerRequestMessengerLinkToken.objects.create",
    ],
)
def test_telegram_persistence_fault_never_rolls_back_the_customer_request(
    public_client, public_catalog, settings, monkeypatch, target
):
    """Conversation, outbox and token writes are optional to the request itself."""
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"

    def fail(*args, **kwargs):
        raise IntegrityError("telegram persistence unavailable")

    monkeypatch.setattr(target, fail)
    response, _token, _part = _send(public_client, public_catalog)
    request = CustomerRequest.objects.get()
    public_client.post(reverse("public_catalog_telegram_continue", args=[request.public_id]))

    assert CustomerRequest.objects.count() == 1
    assert response.status_code == 302


def test_max_request_creates_no_telegram_rows(public_client, public_catalog, settings):
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    settings.MAX_BOT_USERNAME = ""  # MAX not configured on this runtime

    response, _token, _part = _send(public_client, public_catalog, messenger="max")

    page = public_client.get(response["Location"]).content.decode()
    assert CustomerRequest.objects.get().preferred_messenger == "max"
    assert not TelegramConversation.objects.exists()
    assert not TelegramOutboxEvent.objects.exists()
    assert not CustomerRequestMessengerLinkToken.objects.exists()
    assert "Продолжить в Telegram" not in page
    # An unconfigured MAX bot is said plainly, and the request is kept.
    assert "Продолжить в MAX" not in page
    assert "data-max-unavailable" in page


def test_retry_of_a_sent_form_keeps_one_request_one_link_and_one_notification(
    public_client, public_catalog, settings
):
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    response, token, _part = _send(public_client, public_catalog)
    first_page = public_client.get(response["Location"]).content.decode()
    request = CustomerRequest.objects.get()
    continued = public_client.post(
        reverse("public_catalog_telegram_continue", args=[request.public_id])
    )
    assert continued.status_code == 303

    retry = public_client.post(
        "/request/submit/",
        {
            "submission_key": token,
            "customer_name": "Ольга",
            "customer_phone": "+7 (912) 555-44-33",
            "preferred_messenger": "telegram",
            "consent": "1",
        },
    )
    second_page = public_client.get(retry["Location"]).content.decode()

    assert CustomerRequest.objects.count() == 1
    assert TelegramOutboxEvent.objects.count() == 1
    assert CustomerRequestMessengerLinkToken.objects.count() == 1
    assert LINK_RE.search(first_page) is None
    assert LINK_RE.search(second_page) is None


def test_another_browser_never_sees_the_link(public_client, public_catalog, settings, client):
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    response, _token, _part = _send(public_client, public_catalog)
    request = CustomerRequest.objects.get()
    continued = public_client.post(
        reverse("public_catalog_telegram_continue", args=[request.public_id])
    )
    start = parse_qs(urlparse(continued["Location"]).query)["start"][0]
    session = public_client.session
    assert start not in str(dict(session.items()))
    assert start not in public_client.get(response["Location"]).content.decode()

    public_client.cookies.clear()
    assert public_client.get(response["Location"]).status_code == 404
