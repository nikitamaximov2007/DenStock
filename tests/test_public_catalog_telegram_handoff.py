"""The browser handoff from the success page to Telegram.

Production defect (request 0F336A92, Chrome on macOS): the page carried
``form-action 'self'``, so the browser silently refused the POST -> redirect to
t.me, the customer stayed on the success page and the one-shot flag then hid
the button. These tests pin the browser contract (the page must allow the
deep-link origin as a form-action source and answer with a top-level redirect),
the bounded retry, and every secrecy property of the token.
"""

import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.catalog.public_requests import (
    MAX_TELEGRAM_LINK_ATTEMPTS,
    TELEGRAM_SESSION_KEY,
    _link_attempts,
    telegram_success,
)
from apps.customer_requests.messengers import (
    MessengerLinkError,
    consume_telegram_start,
    telegram_deep_link_origin,
)
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestMessengerLinkToken,
    TelegramConversation,
)
from tests.public_catalog_support import PUBLIC_HOST
from tests.test_public_catalog_telegram_request import _send

CUSTOMER_CHAT = 700501
OTHER_CHAT = 700502


@pytest.fixture(autouse=True)
def _reset_public_request_rate_limit():
    """Each test is a fresh visitor: the public form counts sends per client."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def telegram_request(public_client, public_catalog, settings):
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    settings.TELEGRAM_DEEP_LINK_BASE_URL = "https://t.me"
    response, _token, _part = _send(public_client, public_catalog)
    request = CustomerRequest.objects.get()
    return response["Location"], request


def _continue(client, request):
    return client.post(reverse("public_catalog_telegram_continue", args=[request.public_id]))


def _start_token(response) -> str:
    return parse_qs(urlparse(response["Location"]).query)["start"][0]


def test_success_page_allows_the_deep_link_origin_as_a_form_action_source(
    public_client, telegram_request
):
    success_path, _request = telegram_request

    page = public_client.get(success_path)
    catalog = public_client.get("/")

    policy = page["Content-Security-Policy"]
    assert "form-action 'self' https://t.me" in policy
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    # Only this page may hand over to Telegram.
    assert "form-action 'self';" in catalog["Content-Security-Policy"]
    assert "t.me" not in catalog["Content-Security-Policy"]


def test_handoff_is_a_top_level_redirect_to_the_deep_link_origin(public_client, telegram_request):
    success_path, request = telegram_request

    response = _continue(public_client, request)

    assert response.status_code == 303  # a GET navigation away from this POST
    assert response["Location"].startswith("https://t.me/ProStorTestBot?start=")
    assert len(_start_token(response)) == 43


def test_no_raw_token_in_the_page_cookies_or_session(public_client, telegram_request):
    success_path, request = telegram_request
    before = public_client.get(success_path).content.decode()

    token = _start_token(_continue(public_client, request))
    after = public_client.get(success_path).content.decode()

    assert token not in before and token not in after
    assert "t.me/" not in before and "?start=" not in before
    assert token not in str(dict(public_client.session.items()))
    assert all(token not in cookie.value for cookie in public_client.cookies.values())


def test_token_is_stored_hashed_only(public_client, telegram_request):
    _success_path, request = telegram_request

    token = _start_token(_continue(public_client, request))

    stored = CustomerRequestMessengerLinkToken.objects.get(request=request)
    assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert not CustomerRequestMessengerLinkToken.objects.filter(token_hash=token).exists()


def test_reloading_the_success_page_never_creates_a_token(public_client, telegram_request):
    success_path, request = telegram_request

    for _ in range(3):
        assert public_client.get(success_path).status_code == 200

    assert not CustomerRequestMessengerLinkToken.objects.filter(request=request).exists()


def test_customer_can_retry_a_handoff_the_browser_did_not_complete(public_client, telegram_request):
    success_path, request = telegram_request

    tokens = []
    for attempt in range(MAX_TELEGRAM_LINK_ATTEMPTS):
        response = _continue(public_client, request)
        assert response.status_code == 303, attempt
        tokens.append(_start_token(response))
        page = public_client.get(success_path).content.decode()
        attempts_left = MAX_TELEGRAM_LINK_ATTEMPTS - (attempt + 1)
        # While attempts remain the customer keeps a way back to Telegram.
        assert ("Продолжить в Telegram</button>" in page) is (attempts_left > 0), attempt
        assert ("data-telegram-retry" in page) is (attempts_left > 0), attempt

    assert len(set(tokens)) == MAX_TELEGRAM_LINK_ATTEMPTS
    assert CustomerRequestMessengerLinkToken.objects.filter(request=request).count() == (
        MAX_TELEGRAM_LINK_ATTEMPTS
    )

    exhausted = _continue(public_client, request)
    assert exhausted.status_code == 302
    assert exhausted["Location"] == success_path
    page = public_client.get(success_path).content.decode()
    assert "data-telegram-exhausted" in page
    assert "Продолжить в Telegram</button>" not in page
    assert CustomerRequestMessengerLinkToken.objects.filter(request=request).count() == (
        MAX_TELEGRAM_LINK_ATTEMPTS
    )
    assert "Content-Security-Policy" in public_client.get(success_path)
    assert "t.me" not in public_client.get(success_path)["Content-Security-Policy"]


def test_consuming_one_link_revokes_the_requests_other_unused_links(
    public_client, telegram_request
):
    _success_path, request = telegram_request
    first = _start_token(_continue(public_client, request))
    second = _start_token(_continue(public_client, request))

    consume_telegram_start(token=second, chat_id=CUSTOMER_CHAT, user_id=CUSTOMER_CHAT)

    conversation = TelegramConversation.objects.get(request=request)
    assert conversation.is_linked and conversation.customer_chat_id == CUSTOMER_CHAT
    with pytest.raises(MessengerLinkError):
        consume_telegram_start(token=first, chat_id=OTHER_CHAT, user_id=OTHER_CHAT)
    conversation.refresh_from_db()
    assert conversation.customer_chat_id == CUSTOMER_CHAT
    assert CustomerRequestMessengerLinkToken.objects.filter(
        request=request, revoked_at__isnull=False
    ).count() == 1


def test_csrf_is_enforced_on_the_handoff(public_client, telegram_request):
    _success_path, request = telegram_request
    public_client.handler.enforce_csrf_checks = True

    refused = public_client.post(
        reverse("public_catalog_telegram_continue", args=[request.public_id])
    )

    assert refused.status_code == 403
    assert not CustomerRequestMessengerLinkToken.objects.filter(request=request).exists()


def test_another_browser_cannot_use_the_handoff(public_client, telegram_request):
    _success_path, request = telegram_request
    stranger_browser = Client(HTTP_HOST=PUBLIC_HOST)

    stranger = stranger_browser.post(
        reverse("public_catalog_telegram_continue", args=[request.public_id])
    )

    assert stranger.status_code == 404
    assert not CustomerRequestMessengerLinkToken.objects.filter(request=request).exists()


@pytest.mark.parametrize(
    "base", ["", "http://evil.example", "javascript:alert(1)", "https://t.me/path", "t.me"]
)
def test_an_unusable_deep_link_origin_never_reaches_the_page(
    public_client, telegram_request, settings, base
):
    success_path, request = telegram_request
    settings.TELEGRAM_DEEP_LINK_BASE_URL = base

    page = public_client.get(success_path)

    assert telegram_deep_link_origin() == ""
    assert "t.me" not in page["Content-Security-Policy"]
    assert "form-action 'self';" in page["Content-Security-Policy"]
    body = page.content.decode()
    assert "Продолжить в Telegram</button>" not in body
    assert "Telegram сейчас недоступен" in body
    assert _continue(public_client, request)["Location"] == success_path


def test_a_session_written_by_the_old_one_shot_flow_counts_as_one_attempt(settings):
    """A cookie from the deployed flow keeps its meaning: one link already issued."""
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    settings.TELEGRAM_DEEP_LINK_BASE_URL = "https://t.me"
    public_id = "0f336a92-9a23-46c8-9caa-3ab0b0387c6a"
    legacy = {
        "request": public_id,
        "messenger": CustomerRequest.Messenger.TELEGRAM,
        "link_issued": True,
    }

    assert _link_attempts(legacy) == 1
    assert _link_attempts({"request": public_id, "link_attempts": 2}) == 2
    assert _link_attempts({"request": public_id}) == 0

    page = telegram_success({TELEGRAM_SESSION_KEY: legacy}, public_id)
    assert page["telegram_can_continue"] is True
    assert page["telegram_retry"] is True
    assert page["telegram_attempts_left"] == MAX_TELEGRAM_LINK_ATTEMPTS - 1

    exhausted = dict(legacy, link_attempts=MAX_TELEGRAM_LINK_ATTEMPTS)
    spent = telegram_success({TELEGRAM_SESSION_KEY: exhausted}, public_id)
    assert spent["telegram_can_continue"] is False
    assert spent["telegram_attempts_left"] == 0
