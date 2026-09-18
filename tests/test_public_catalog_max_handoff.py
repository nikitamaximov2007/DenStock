"""PRO-STOR -> MAX: the public handoff, and the whole local end-to-end flow.

The handoff repeats the Telegram contract that production taught us: the
success page allows exactly the deep-link origin of *this request's* messenger
as a ``form-action`` source (Chromium applies it to the whole redirect chain),
answers the local POST with a 303 to ``https://max.ru/<bot>?start=<token>``,
never renders or stores the raw token, and keeps a bounded way back if the
browser did not leave the page.

The end-to-end tests then drive a customer from the public catalog through
MAX (``FakeMaxServer`` over real HTTP, the real webhook view with its secret)
to an operator's reply and back, including a returning customer with two
requests and a cancellation.
"""

import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.public_requests import (
    MAX_MESSENGER_LINK_ATTEMPTS,
    MESSENGER_SESSION_KEY,
    max_success,
)
from apps.customer_requests import messaging, views
from apps.customer_requests.max_api import MaxBotApi
from apps.customer_requests.max_bot import MaxBotWorker, SendPacer
from apps.customer_requests.max_service import (
    AMBIGUOUS_TEXT,
    LINK_INVALID_TEXT,
    SELECTED_TEXT,
)
from apps.customer_requests.messengers import max_deep_link_origin
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestMessengerLinkToken,
    MaxConversation,
    MaxCustomerChat,
    MaxDeliveryStatus,
    MaxMessage,
    MaxOutboxEvent,
    TelegramConversation,
    TelegramOperator,
)
from apps.customer_requests.services import change_request_status
from apps.customer_requests.telegram_bot import TelegramBotWorker
from apps.operations.models import MaxBotRuntime
from tests.public_catalog_support import PUBLIC_HOST
from tests.test_public_catalog_telegram_request import _prepare, _send
from tests.test_telegram_customer_messaging import FakeBotApi

from .max_fake import (
    FAKE_BOT_USERNAME,
    FAKE_MAX_TOKEN,
    FAKE_WEBHOOK_SECRET,
    FakeMaxServer,
    bot_started,
    message_callback,
    message_created,
)

CUSTOMER = 42_000_001
CUSTOMER_CHAT = 52_000_001
OTHER = 42_000_002
OTHER_CHAT = 52_000_002


@pytest.fixture(autouse=True)
def _fresh_visitor():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def max_configured(settings):
    settings.MAX_BOT_USERNAME = FAKE_BOT_USERNAME
    settings.MAX_DEEP_LINK_BASE_URL = "https://max.ru"
    settings.TELEGRAM_BOT_USERNAME = "ProStorTestBot"
    settings.TELEGRAM_DEEP_LINK_BASE_URL = "https://t.me"
    settings.MAX_WEBHOOK_ENABLED = True
    settings.MAX_WEBHOOK_SECRET = FAKE_WEBHOOK_SECRET
    settings.TELEGRAM_INTERNAL_BASE_URL = "https://denstock.example"
    return settings


@pytest.fixture
def max_request(public_client, public_catalog, max_configured):
    response, _token, _part = _send(public_client, public_catalog, messenger="max")
    return response["Location"], CustomerRequest.objects.get()


def _continue(client, request):
    return client.post(reverse("public_catalog_max_continue", args=[request.public_id]))


def _start_token(response) -> str:
    return parse_qs(urlparse(response["Location"]).query)["start"][0]


# --- 46-51: the public handoff ------------------------------------------------------------


def test_request_form_offers_max(public_client, public_catalog, max_configured):
    _prepare(public_client, public_catalog)
    form = public_client.get("/request/").content.decode()
    assert 'value="max"' in form
    assert "MAX" in form


def test_success_page_offers_continue_in_max_without_any_secret(public_client, max_request):
    success_path, request = max_request

    page = public_client.get(success_path)
    body = page.content.decode()

    assert request.preferred_messenger == "max"
    assert "Продолжить в MAX</button>" in body
    assert "Продолжить в Telegram" not in body
    assert "?start=" not in body and "max.ru/" not in body
    assert FAKE_MAX_TOKEN not in body
    assert not CustomerRequestMessengerLinkToken.objects.exists()  # nothing before the click
    # The public role writes no MAX table.
    assert not MaxConversation.objects.exists() and not MaxOutboxEvent.objects.exists()
    assert not TelegramConversation.objects.exists()


def test_success_page_allows_exactly_this_requests_messenger_origin(
    public_client, public_catalog, max_request
):
    success_path, _request = max_request

    policy = public_client.get(success_path)["Content-Security-Policy"]
    catalog = public_client.get("/")["Content-Security-Policy"]

    assert "form-action 'self' https://max.ru;" in policy
    assert "t.me" not in policy
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    assert "form-action 'self';" in catalog and "max.ru" not in catalog


def test_telegram_request_success_page_does_not_allow_max(public_client, public_catalog,
                                                          max_configured):
    response, _token, _part = _send(public_client, public_catalog, messenger="telegram")
    policy = public_client.get(response["Location"])["Content-Security-Policy"]
    assert "form-action 'self' https://t.me;" in policy
    assert "max.ru" not in policy


def test_handoff_is_a_303_to_the_max_deep_link(public_client, max_request):
    _success_path, request = max_request

    response = _continue(public_client, request)

    assert response.status_code == 303
    assert response["Location"].startswith(f"https://max.ru/{FAKE_BOT_USERNAME}?start=")
    token = _start_token(response)
    assert len(token) == 43 <= 128
    row = CustomerRequestMessengerLinkToken.objects.get(request=request)
    assert row.channel == "max"
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()


def test_no_raw_token_in_page_cookie_or_session(public_client, max_request):
    success_path, request = max_request
    before = public_client.get(success_path).content.decode()

    token = _start_token(_continue(public_client, request))
    after = public_client.get(success_path).content.decode()

    assert token not in before and token not in after
    assert token not in str(dict(public_client.session.items()))
    assert all(token not in cookie.value for cookie in public_client.cookies.values())
    assert not CustomerRequestMessengerLinkToken.objects.filter(token_hash=token).exists()


def test_bounded_retry_keeps_a_way_back_then_stops(public_client, max_request):
    success_path, request = max_request
    tokens = []
    for attempt in range(MAX_MESSENGER_LINK_ATTEMPTS):
        response = _continue(public_client, request)
        assert response.status_code == 303, attempt
        tokens.append(_start_token(response))
        page = public_client.get(success_path).content.decode()
        left = MAX_MESSENGER_LINK_ATTEMPTS - attempt - 1
        assert ("Продолжить в MAX</button>" in page) is (left > 0)
        assert ("data-max-retry" in page) is (left > 0)

    assert len(set(tokens)) == MAX_MESSENGER_LINK_ATTEMPTS
    exhausted = _continue(public_client, request)
    assert exhausted.status_code == 302 and exhausted["Location"] == success_path
    page = public_client.get(success_path)
    assert "data-max-exhausted" in page.content.decode()
    assert "max.ru" not in page["Content-Security-Policy"]
    assert CustomerRequestMessengerLinkToken.objects.filter(request=request).count() == 3


def test_handoff_refuses_other_browsers_and_missing_csrf(public_client, max_request):
    _success_path, request = max_request
    stranger = Client(HTTP_HOST=PUBLIC_HOST)
    assert _continue(stranger, request).status_code == 404
    public_client.handler.enforce_csrf_checks = True
    assert _continue(public_client, request).status_code == 403
    assert not CustomerRequestMessengerLinkToken.objects.exists()


def test_a_telegram_request_cannot_mint_a_max_link(public_client, public_catalog, max_configured):
    response, _token, _part = _send(public_client, public_catalog, messenger="telegram")
    request = CustomerRequest.objects.get()
    refused = _continue(public_client, request)
    assert refused.status_code == 302
    assert not CustomerRequestMessengerLinkToken.objects.filter(channel="max").exists()


@pytest.mark.parametrize("base", ["", "http://evil.example", "https://max.ru/x", "max.ru"])
def test_an_unusable_max_origin_never_reaches_the_page(public_client, max_request, settings, base):
    success_path, request = max_request
    settings.MAX_DEEP_LINK_BASE_URL = base

    page = public_client.get(success_path)

    assert max_deep_link_origin() == ""
    assert "form-action 'self';" in page["Content-Security-Policy"]
    assert "Продолжить в MAX</button>" not in page.content.decode()
    assert "data-max-unavailable" in page.content.decode()
    assert _continue(public_client, request)["Location"] == success_path


def test_max_state_reads_only_this_browsers_cookie(max_configured):
    public_id = "7a2b1c9d-1111-4222-8333-944455556666"
    state = {"request": public_id, "messenger": "max", "link_attempts": 1}
    page = max_success({MESSENGER_SESSION_KEY: state}, public_id)
    assert page["max_can_continue"] and page["max_retry"]
    assert max_success({MESSENGER_SESSION_KEY: state}, "other")["max_selected"] is False
    telegram_state = dict(state, messenger="telegram")
    assert max_success({MESSENGER_SESSION_KEY: telegram_state}, public_id)["max_selected"] is False


# --- 25-27 of Part 13: the local end-to-end flow ----------------------------------------


INTERNAL_URLCONF = "config.urls"


class Internal:
    """The internal runtime's views, called directly next to the public client.

    Separate processes in production; here they run with the internal URLconf
    while the public client keeps its own.
    """

    def __init__(self):
        self.factory = RequestFactory()

    @override_settings(ROOT_URLCONF=INTERNAL_URLCONF)
    def webhook(self, update, secret=FAKE_WEBHOOK_SECRET):
        request = self.factory.post(
            "/customer-requests/max/webhook/",
            data=json.dumps(update, ensure_ascii=False),
            content_type="application/json",
            HTTP_X_MAX_BOT_API_SECRET=secret,
        )
        response = views.max_webhook(request)
        assert response.status_code == 200, response.status_code
        assert json.loads(response.content) == {"ok": True}
        return response

    @override_settings(ROOT_URLCONF=INTERNAL_URLCONF)
    def reply(self, user, request_pk, text, key):
        request = self.factory.post(
            f"/customer-requests/{request_pk}/max-reply/",
            {"text": text, "submission_key": key},
        )
        request.user = user
        request.session = {}
        request._messages = FallbackStorage(request)
        response = views.customer_request_max_reply(request, request_pk)
        assert response.status_code == 302
        return response


@pytest.fixture
def e2e(public_client, public_catalog, max_configured, django_user_model):
    server = FakeMaxServer()
    server.start()
    worker = MaxBotWorker(
        MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=2),
        worker_id="e2e",
        heartbeat_file="",
        pacer=SendPacer(sleep=lambda seconds: None),
    )
    worker.start()
    MaxBotRuntime.objects.update(announce_requests_since=timezone.now())
    users = []
    for telegram_id, username in ((830001, "denis"), (830002, "masha")):
        user = django_user_model.objects.create_user(username=username, password="x" * 12)
        user.groups.add(Group.objects.get(name=roles.SELLER))
        TelegramOperator.objects.create(user=user, telegram_user_id=telegram_id)
        users.append(user)
    tg_api = FakeBotApi()
    operator_bot = TelegramBotWorker(tg_api, worker_id="e2e-tg", poll_timeout=0, heartbeat_file="")
    operator_bot.start()

    @override_settings(ROOT_URLCONF=INTERNAL_URLCONF)
    def cycle():
        for _ in range(2):
            worker.iterate()
            operator_bot.iterate(poll_timeout=0)

    yield {
        "server": server,
        "worker": worker,
        "cycle": cycle,
        "internal": Internal(),
        "operators": users,
        "tg": tg_api,
        "client": public_client,
        "catalog": public_catalog,
    }
    server.stop()


def _new_request_through_the_catalog(e2e):
    client, catalog = e2e["client"], e2e["catalog"]
    response, _token, _part = _send(client, catalog, messenger="max")
    request = CustomerRequest.objects.order_by("-pk").first()
    assert "Продолжить в MAX</button>" in client.get(response["Location"]).content.decode()
    handoff = _continue(client, request)
    assert handoff.status_code == 303
    return request, _start_token(handoff)


def _customer_texts(request):
    return list(
        MaxMessage.objects.filter(
            conversation__request=request, direction=MaxMessage.Direction.CUSTOMER
        ).values_list("text", flat=True)
    )


def test_end_to_end_first_time_customer(e2e):
    server, cycle, internal, tg = e2e["server"], e2e["cycle"], e2e["internal"], e2e["tg"]
    denis, masha = e2e["operators"]

    # 1-2: request with MAX, Continue in MAX.
    request, token = _new_request_through_the_catalog(e2e)
    cycle()
    assert any("НОВАЯ ЗАЯВКА" in text for text in tg.texts_to(830001))

    # 3-5: MAX start with the payload binds the right request and sends the summary.
    start = bot_started(CUSTOMER, CUSTOMER_CHAT, token)
    internal.webhook(start)
    internal.webhook(start)  # MAX redelivers
    cycle()
    conversation = MaxConversation.objects.get(request=request)
    assert conversation.is_linked and conversation.customer_user_id == CUSTOMER
    texts = server.texts_to(CUSTOMER_CHAT)
    assert len(texts) == 1
    assert texts[0].startswith(f"Добрый день! Ваша заявка №{request.reference} получена.")
    assert "Итого:" in texts[0] or "уточняется" in texts[0]

    # 6-8: first message, exactly one ACK, operators get it.
    first = message_created(CUSTOMER, CUSTOMER_CHAT, "Можно забрать завтра?")
    internal.webhook(first)
    internal.webhook(first)
    cycle()
    assert server.texts_to(CUSTOMER_CHAT).count(messaging.CUSTOMER_ACK_TEXT) == 1
    for telegram_id in (830001, 830002):
        assert any("Можно забрать завтра?" in text for text in tg.texts_to(telegram_id))

    # 9-10: second message, no second ACK, operators get it too.
    internal.webhook(message_created(CUSTOMER, CUSTOMER_CHAT, "И ещё ремень, если есть"))
    cycle()
    assert server.texts_to(CUSTOMER_CHAT).count(messaging.CUSTOMER_ACK_TEXT) == 1
    assert _customer_texts(request) == ["Можно забрать завтра?", "И ещё ремень, если есть"]
    for telegram_id in (830001, 830002):
        assert any("И ещё ремень, если есть" in text for text in tg.texts_to(telegram_id))

    # 11-12: an operator replies in DenisStock; the customer receives it exactly once.
    key = "c0ffee" + "0" * 26
    internal.reply(denis, request.pk, "Завтра с 10 до 19, ремень тоже есть.", key)
    internal.reply(denis, request.pk, "Завтра с 10 до 19, ремень тоже есть.", key)
    cycle()
    cycle()
    assert server.texts_to(CUSTOMER_CHAT).count("Завтра с 10 до 19, ремень тоже есть.") == 1
    reply = MaxMessage.objects.get(direction=MaxMessage.Direction.OPERATOR)
    assert reply.operator_user == denis and reply.delivery_status == MaxDeliveryStatus.SENT
    assert any("Ответ клиенту отправлен" in text for text in tg.texts_to(830002))
    assert not any("Ответ клиенту отправлен" in text for text in tg.texts_to(830001))
    assert "denis" not in "\n".join(server.texts_to(CUSTOMER_CHAT))
    assert not MaxMessage.objects.exclude(
        direction=MaxMessage.Direction.CUSTOMER
    ).exclude(delivery_status=MaxDeliveryStatus.SENT).exists()


def test_end_to_end_returning_customer_and_cancellation(e2e):
    server, cycle, internal = e2e["server"], e2e["cycle"], e2e["internal"]

    request_a, token_a = _new_request_through_the_catalog(e2e)
    internal.webhook(bot_started(CUSTOMER, CUSTOMER_CHAT, token_a))
    internal.webhook(message_created(CUSTOMER, CUSTOMER_CHAT, "Вопрос по A"))
    cycle()

    # 13-14: same MAX identity, a second request, no second account.
    request_b, token_b = _new_request_through_the_catalog(e2e)
    internal.webhook(message_created(CUSTOMER, CUSTOMER_CHAT, f"/start {token_b}"))
    cycle()
    conversation_a = MaxConversation.objects.get(request=request_a)
    conversation_b = MaxConversation.objects.get(request=request_b)
    assert conversation_b.customer_user_id == CUSTOMER == conversation_a.customer_user_id
    assert MaxCustomerChat.objects.filter(user_id=CUSTOMER).count() == 1

    # 15: the selection flow; with no deterministic selection nothing is guessed.
    MaxCustomerChat.objects.update(active_conversation=None)
    internal.webhook(message_created(CUSTOMER, CUSTOMER_CHAT, "Это к какой заявке?"))
    cycle()
    assert server.sent[-1]["text"] == AMBIGUOUS_TEXT
    assert "Это к какой заявке?" not in _customer_texts(request_a) + _customer_texts(request_b)

    # 16-17: select B -> B only.
    internal.webhook(message_callback(CUSTOMER, CUSTOMER_CHAT, f"s:{conversation_b.public_id.hex}"))
    internal.webhook(message_created(CUSTOMER, CUSTOMER_CHAT, "Для B"))
    cycle()
    assert server.texts_to(CUSTOMER_CHAT)[-2] == SELECTED_TEXT.format(
        reference=request_b.reference
    )
    assert _customer_texts(request_b) == ["Для B"]
    assert _customer_texts(request_a) == ["Вопрос по A"]

    # 18-19: select A -> A only.
    internal.webhook(message_callback(CUSTOMER, CUSTOMER_CHAT, f"s:{conversation_a.public_id.hex}"))
    internal.webhook(message_created(CUSTOMER, CUSTOMER_CHAT, "Для A"))
    cycle()
    assert _customer_texts(request_a) == ["Вопрос по A", "Для A"]
    assert _customer_texts(request_b) == ["Для B"]
    # Each request acknowledged its own first message once.
    assert server.texts_to(CUSTOMER_CHAT).count(messaging.CUSTOMER_ACK_TEXT) == 2

    # 20-21: a cancelled request cannot be rebound, and its history stays.
    boss = e2e["operators"][0]
    change_request_status(request_id=request_b.pk, target_status="canceled", by=boss)
    request_c_token = CustomerRequestMessengerLinkToken.objects.filter(request=request_b)
    assert request_c_token.filter(used_at__isnull=True, revoked_at__isnull=True).count() == 0
    internal.webhook(bot_started(OTHER, OTHER_CHAT, token_b))
    cycle()
    assert server.texts_to(OTHER_CHAT) == [LINK_INVALID_TEXT]
    conversation_b.refresh_from_db()
    assert conversation_b.customer_user_id == CUSTOMER
    assert _customer_texts(request_b) == ["Для B"]
