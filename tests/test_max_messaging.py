"""MAX messaging for customer requests: webhook, binding, routing, operators, worker.

MAX itself is ``FakeMaxServer`` over real local HTTP, driven by the production
``MaxBotApi`` client. Updates enter through the real webhook view with the
real secret check. Operator notifications leave through the real Telegram
operator worker with its fake Bot API. No internet, no real token or secret.
"""

import hashlib
import logging
import re
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth.models import Group
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import PartNumber, PartType
from apps.customer_requests import max_bot, max_service, messaging
from apps.customer_requests.max_api import MaxBotApi
from apps.customer_requests.max_bot import MAX_ATTEMPTS, MaxBotWorker, SendPacer
from apps.customer_requests.messengers import MessengerLinkError, issue_max_link
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestLine,
    CustomerRequestMessengerLinkToken,
    MaxConversation,
    MaxCustomerChat,
    MaxDeliveryStatus,
    MaxMessage,
    MaxOperatorDelivery,
    MaxOutboxEvent,
    TelegramConversation,
    TelegramOperator,
)
from apps.customer_requests.services import (
    RequestLineInput,
    anonymize_request,
    change_request_status,
    create_customer_request,
    withdraw_consent,
)
from apps.customer_requests.telegram_bot import TelegramBotWorker
from apps.operations.models import MaxBotRuntime

from .max_fake import (
    FAKE_MAX_TOKEN,
    FAKE_WEBHOOK_SECRET,
    FakeMaxServer,
    bot_started,
    deliver,
    message_callback,
    message_created,
)
from .test_customer_requests import POLICY
from .test_telegram_customer_messaging import FakeBotApi

CUSTOMER = 41_000_001
CUSTOMER_CHAT = 51_000_001
OTHER = 41_000_002
OTHER_CHAT = 51_000_002
OPERATOR_A_TG = 810001
OPERATOR_B_TG = 810002


# --- Fixtures ------------------------------------------------------------------------------


@pytest.fixture
def part(db):
    from apps.catalog.models import Category, Manufacturer, Unit

    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    result = PartType.objects.create(
        name="РЕМЕНЬ ВАРИАТОРА",
        category=category,
        unit=Unit.objects.get(name="Штука"),
        manufacturer=Manufacturer.objects.get_or_create(name="BRP")[0],
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("12500.00"),
        certified_price_rub=Decimal("12500.00"),
        price_provenance=PartType.PriceProvenance.FORMULA_CERTIFIED,
    )
    PartNumber.objects.create(part=result, value="417300571", is_primary=True)
    return result


@pytest.fixture(autouse=True)
def max_settings(settings):
    settings.MAX_WEBHOOK_ENABLED = True
    settings.MAX_WEBHOOK_SECRET = FAKE_WEBHOOK_SECRET
    settings.MAX_BOT_USERNAME = "id0000000000_bot"
    settings.MAX_DEEP_LINK_BASE_URL = "https://max.ru"
    settings.TELEGRAM_INTERNAL_BASE_URL = "https://denstock.example"
    return settings


@pytest.fixture
def server():
    fake = FakeMaxServer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def worker(db, server, sleeps):
    api = MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=2)
    bot = MaxBotWorker(
        api, worker_id="max-a", heartbeat_file="", pacer=SendPacer(sleep=sleeps.append)
    )
    bot.start()
    # Announce every MAX request created in these tests.
    MaxBotRuntime.objects.update(announce_requests_since=timezone.now() - timedelta(days=1))
    return bot


def _operator(django_user_model, telegram_id, *, username):
    user = django_user_model.objects.create_user(
        username=username, password="x" * 12, first_name=username.title()
    )
    user.groups.add(Group.objects.get(name=roles.SELLER))
    return TelegramOperator.objects.create(user=user, telegram_user_id=telegram_id)


@pytest.fixture
def operators(db, django_user_model):
    return (
        _operator(django_user_model, OPERATOR_A_TG, username="denis"),
        _operator(django_user_model, OPERATOR_B_TG, username="masha"),
    )


@pytest.fixture
def operator_bot(db):
    api = FakeBotApi()
    bot = TelegramBotWorker(api, worker_id="tg-a", poll_timeout=0, heartbeat_file="")
    bot.start()
    return bot, api


def _request(part, *, key, price=True, lines=None):
    request, created = create_customer_request(
        customer_name="Ольга Смирнова",
        customer_phone="+7 (912) 555-44-33",
        preferred_messenger=CustomerRequest.Messenger.MAX,
        comment="Нужна до пятницы.",
        lines=lines or [RequestLineInput(part_id=part.pk, quantity="2", supply_inquiry=True)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=key,
    )
    assert created
    if not price:
        CustomerRequestLine.objects.filter(request=request).update(price_seen=None)
    return request


def drain(worker):
    worker.iterate()
    # A second cycle proves a repeated drain adds nothing.
    worker.iterate()


def bind(client, worker, request, *, user=CUSTOMER, chat=CUSTOMER_CHAT):
    token = issue_max_link(request_id=request.pk).token
    assert deliver(client, bot_started(user, chat, token)).status_code == 200
    drain(worker)
    return token


def say(client, worker, text, *, user=CUSTOMER, chat=CUSTOMER_CHAT, mid=None):
    update = message_created(user, chat, text, mid=mid)
    assert deliver(client, update).status_code == 200
    drain(worker)
    return update


def press(client, worker, conversation, *, user=CUSTOMER, chat=CUSTOMER_CHAT):
    update = message_callback(user, chat, f"s:{conversation.public_id.hex}")
    assert deliver(client, update).status_code == 200
    drain(worker)
    return update


def customer_messages(request):
    return list(
        MaxMessage.objects.filter(
            conversation__request=request, direction=MaxMessage.Direction.CUSTOMER
        ).values_list("text", flat=True)
    )


# --- Webhook: 13-21 ------------------------------------------------------------------------


def test_webhook_with_the_right_secret_answers_ok_and_nothing_else(client, part, worker):
    request = _request(part, key="w" * 32)
    token = issue_max_link(request_id=request.pk).token

    response = deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert MaxConversation.objects.get(request=request).is_linked


@pytest.mark.parametrize("secret", ["wrong_secret_value", "", None])
def test_webhook_rejects_a_wrong_or_missing_secret_without_side_effects(
    client, part, worker, secret
):
    request = _request(part, key="x" * 32)
    token = issue_max_link(request_id=request.pk).token

    response = deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token), secret=secret)

    assert response.status_code == 404
    assert not MaxConversation.objects.filter(request=request, status="linked").exists()
    assert not MaxMessage.objects.exists()
    assert CustomerRequestMessengerLinkToken.objects.get(request=request).used_at is None


def test_webhook_is_closed_when_disabled_or_the_secret_is_unusable(client, settings, db):
    update = message_created(CUSTOMER, CUSTOMER_CHAT, "Здравствуйте")
    settings.MAX_WEBHOOK_ENABLED = False
    assert deliver(client, update).status_code == 404
    settings.MAX_WEBHOOK_ENABLED = True
    settings.MAX_WEBHOOK_SECRET = ""
    assert deliver(client, update, secret="").status_code == 404
    settings.MAX_WEBHOOK_SECRET = "bad secret!"
    assert deliver(client, update, secret="bad secret!").status_code == 404
    assert not MaxMessage.objects.exists()


def test_webhook_accepts_post_only(client, db):
    assert client.get("/customer-requests/max/webhook/").status_code == 405


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b"[1, 2]", b'"text"', b"{}", b'{"update_type": 5}', "\udcff".encode(
        "utf-8", "surrogatepass")],
)
def test_malformed_bodies_are_refused_safely(client, db, raw):
    response = deliver(client, None, raw=raw)
    assert response.status_code == 400
    assert not MaxMessage.objects.exists()


def test_an_oversized_body_is_refused(client, db):
    update = message_created(CUSTOMER, CUSTOMER_CHAT, "я" * 40000)
    assert deliver(client, update).status_code == 400


@pytest.mark.parametrize(
    "update",
    [
        {"update_type": "message_edited", "timestamp": 1},
        {"update_type": "bot_stopped", "timestamp": 1, "chat_id": 1, "user": {"user_id": 1}},
        {"update_type": "dialog_cleared", "timestamp": 1},
        {"update_type": "message_created", "timestamp": 1},
        {"update_type": "message_callback", "timestamp": 1, "callback": "nope"},
    ],
)
def test_unsupported_or_incomplete_updates_are_accepted_and_ignored(client, db, update):
    response = deliver(client, update)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert not MaxMessage.objects.exists()


def test_group_chats_and_bots_never_become_customers(client, part, worker):
    request = _request(part, key="g" * 32)
    bind(client, worker, request)
    before = MaxMessage.objects.count()

    deliver(client, message_created(CUSTOMER, CUSTOMER_CHAT, "в группе", chat_type="chat"))
    deliver(client, message_created(CUSTOMER, CUSTOMER_CHAT, "я бот", is_bot=True))

    assert MaxMessage.objects.count() == before


def test_duplicate_webhook_delivery_is_idempotent(client, part, worker, server, operators):
    request = _request(part, key="d" * 32)
    bind(client, worker, request)
    update = message_created(CUSTOMER, CUSTOMER_CHAT, "Когда можно забрать?")

    for _ in range(3):
        assert deliver(client, update).status_code == 200
    drain(worker)

    assert customer_messages(request) == ["Когда можно забрать?"]
    assert MaxOutboxEvent.objects.filter(kind="customer_message").count() == 1
    assert server.texts_to(CUSTOMER_CHAT).count(messaging.CUSTOMER_ACK_TEXT) == 1


def test_every_accepted_update_answers_identically(client, part, worker):
    """The webhook is not an oracle: linked, unlinked and invalid look the same."""
    request = _request(part, key="o" * 32)
    token = issue_max_link(request_id=request.pk).token
    bodies = {
        deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token)).content,
        deliver(client, bot_started(OTHER, OTHER_CHAT, "A" * 43)).content,
        deliver(client, message_created(OTHER, OTHER_CHAT, "кто здесь")).content,
        deliver(client, message_callback(OTHER, OTHER_CHAT, "s:" + "0" * 32)).content,
        deliver(client, {"update_type": "user_added", "timestamp": 1}).content,
    }
    assert bodies == {b'{"ok": true}'}


def test_secret_and_payloads_never_reach_the_logs(client, part, worker, caplog):
    request = _request(part, key="l" * 32)
    token = issue_max_link(request_id=request.pk).token
    with caplog.at_level(logging.DEBUG):
        deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))
        deliver(client, message_created(CUSTOMER, CUSTOMER_CHAT, "секретный текст клиента"))
        deliver(client, message_created(CUSTOMER, CUSTOMER_CHAT, "x"), secret="guess_secret")
        with mock.patch.object(max_bot, "handle_update", side_effect=RuntimeError(token)):
            assert deliver(client, message_created(CUSTOMER, CUSTOMER_CHAT, "y")).status_code == 200
    assert FAKE_WEBHOOK_SECRET not in caplog.text
    assert "guess_secret" not in caplog.text
    assert token not in caplog.text
    assert "секретный текст клиента" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_database_fault_asks_max_to_deliver_again(client, part, worker):
    request = _request(part, key="f" * 32)
    token = issue_max_link(request_id=request.pk).token
    update = bot_started(CUSTOMER, CUSTOMER_CHAT, token)
    with mock.patch.object(max_bot, "handle_update", side_effect=DatabaseError("down")):
        assert deliver(client, update).status_code == 503
    # The redelivery then succeeds, exactly once.
    assert deliver(client, update).status_code == 200
    assert deliver(client, update).status_code == 200
    drain(worker)
    assert MaxOutboxEvent.objects.filter(kind="customer_linked").count() == 1


def test_bot_started_without_payload_greets_and_stores_no_identity_for_long(
    client, db, worker, server
):
    deliver(client, bot_started(OTHER, OTHER_CHAT))
    drain(worker)

    assert server.texts_to(OTHER_CHAT) == [max_service.UNLINKED_GREETING]
    assert not MaxCustomerChat.objects.exists()
    MaxMessage.objects.update(created_at=timezone.now() - timedelta(days=2))
    worker.purge_ephemeral()
    assert not MaxMessage.objects.exists()


# --- Binding: 22-28 --------------------------------------------------------------------


def test_start_binds_the_request_and_sends_the_summary_once(client, part, worker, server):
    request = _request(part, key="b" * 32)
    token = issue_max_link(request_id=request.pk).token
    update = bot_started(CUSTOMER, CUSTOMER_CHAT, token)

    deliver(client, update)
    drain(worker)
    deliver(client, update)  # MAX redelivers the very same start
    drain(worker)

    conversation = MaxConversation.objects.get(request=request)
    assert conversation.is_linked
    assert (conversation.customer_user_id, conversation.customer_chat_id) == (
        CUSTOMER, CUSTOMER_CHAT
    )
    assert MaxCustomerChat.objects.get(user_id=CUSTOMER).active_conversation == conversation
    texts = server.texts_to(CUSTOMER_CHAT)
    assert len(texts) == 1
    assert texts[0].startswith(f"Готово. MAX подключён к заявке {request.reference}.")
    assert max_service.LINK_INVALID_TEXT not in texts
    assert MaxOutboxEvent.objects.filter(kind="customer_linked").count() == 1
    assert CustomerRequestMessengerLinkToken.objects.get(request=request).used_at is not None
    assert request.messenger_contact.channel == "max"


def test_token_is_stored_only_as_a_hash(client, part, worker):
    request = _request(part, key="h" * 32)
    token = issue_max_link(request_id=request.pk).token
    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))
    drain(worker)

    row = CustomerRequestMessengerLinkToken.objects.get(request=request)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert len(token) == 43 and len(token) <= 128
    dump = repr(list(MaxMessage.objects.values())) + repr(
        list(CustomerRequestMessengerLinkToken.objects.values())
    )
    assert token not in dump


def test_an_expired_link_is_refused(client, part, worker, server):
    request = _request(part, key="e" * 32)
    token = issue_max_link(request_id=request.pk).token
    CustomerRequestMessengerLinkToken.objects.update(expires_at=timezone.now())

    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))
    drain(worker)

    assert not MaxConversation.objects.get(request=request).is_linked
    assert server.texts_to(CUSTOMER_CHAT) == [max_service.LINK_INVALID_TEXT]


def test_a_link_is_one_use_and_cannot_take_over_a_bound_request(client, part, worker, server):
    request = _request(part, key="u" * 32)
    token = bind(client, worker, request)

    deliver(client, bot_started(OTHER, OTHER_CHAT, token))
    deliver(client, message_created(OTHER, OTHER_CHAT, f"/start {token}"))
    drain(worker)

    conversation = MaxConversation.objects.get(request=request)
    assert conversation.customer_user_id == CUSTOMER
    assert server.texts_to(OTHER_CHAT) == [max_service.LINK_INVALID_TEXT] * 2
    assert not MaxCustomerChat.objects.filter(user_id=OTHER).exists()


def test_consuming_one_link_revokes_the_requests_other_links(client, part, worker):
    request = _request(part, key="s" * 32)
    first = issue_max_link(request_id=request.pk).token
    # A retried handoff: the INSERT-only public path leaves earlier links unrevoked.
    from apps.customer_requests.messengers import issue_initial_messenger_link

    second = issue_initial_messenger_link(request, "max")
    third = issue_initial_messenger_link(request, "max")

    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, second))
    drain(worker)

    rows = CustomerRequestMessengerLinkToken.objects.filter(request=request)
    assert rows.filter(used_at__isnull=False).count() == 1
    assert rows.filter(used_at__isnull=True, revoked_at__isnull=True).count() == 0
    for leftover in (first, third):
        deliver(client, bot_started(OTHER, OTHER_CHAT, leftover))
    drain(worker)
    assert MaxConversation.objects.get(request=request).customer_user_id == CUSTOMER


def test_cancelled_and_withdrawn_requests_are_never_bound(client, part, worker, server,
                                                          admin_user):
    cancelled = _request(part, key="c" * 32)
    token = issue_max_link(request_id=cancelled.pk).token
    change_request_status(request_id=cancelled.pk, target_status="canceled", by=admin_user)
    with pytest.raises(MessengerLinkError):
        issue_max_link(request_id=cancelled.pk)

    withdrawn = _request(part, key="q" * 32)
    withdrawn_token = issue_max_link(request_id=withdrawn.pk).token
    withdraw_consent(request_id=withdrawn.pk)

    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))
    deliver(client, message_created(CUSTOMER, CUSTOMER_CHAT, f"/start {withdrawn_token}"))
    drain(worker)

    assert not MaxConversation.objects.filter(status="linked").exists()
    assert server.texts_to(CUSTOMER_CHAT) == [max_service.LINK_INVALID_TEXT] * 2


def test_a_forged_or_foreign_channel_token_is_refused(client, part, worker, server):
    telegram_request, _ = create_customer_request(
        customer_name="Иван",
        customer_phone="+7 912 000-00-00",
        preferred_messenger="telegram",
        lines=[RequestLineInput(part_id=part.pk, quantity="1", supply_inquiry=True)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key="t" * 32,
    )
    from apps.customer_requests.messengers import issue_telegram_link

    telegram_token = issue_telegram_link(request_id=telegram_request.pk).token
    for payload in (telegram_token, "A" * 43, "короткий", "x" * 200):
        deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, payload))
    drain(worker)

    assert not MaxConversation.objects.exists()
    assert not TelegramConversation.objects.filter(status="linked").exists()
    assert server.texts_to(CUSTOMER_CHAT) == [max_service.LINK_INVALID_TEXT] * 4


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL concurrency integration test"
)
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_starts_with_one_token_bind_exactly_one_user(part):
    request = _request(part, key="k" * 32)
    token = issue_max_link(request_id=request.pk).token
    barrier = threading.Barrier(2)
    outcomes = []

    def start(user):
        barrier.wait()
        try:
            with transaction.atomic():
                max_bot.handle_update(bot_started(user, user + 10, token))
            outcomes.append(user)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=start, args=(user,)) for user in (CUSTOMER, OTHER)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    conversation = MaxConversation.objects.get(request=request)
    assert conversation.customer_user_id in (CUSTOMER, OTHER)
    assert MaxOutboxEvent.objects.filter(kind="customer_linked").count() == 1
    assert MaxMessage.objects.filter(dedupe_key__startswith="summary:").count() == 1


# --- Messaging: 29-39 --------------------------------------------------------------------


def test_same_mid_is_stored_once_and_different_mids_are_different_messages(
    client, part, worker
):
    request = _request(part, key="m" * 32)
    bind(client, worker, request)

    say(client, worker, "Да", mid="mid.AAA")
    say(client, worker, "Да", mid="mid.AAA")
    say(client, worker, "Да", mid="mid.BBB")
    say(client, worker, "Да", mid="mid.aaa")  # ids are case-sensitive strings

    assert customer_messages(request) == ["Да", "Да", "Да"]
    stored = set(
        MaxMessage.objects.filter(direction="customer_to_operator").values_list(
            "external_message_id", flat=True
        )
    )
    assert stored == {"mid.AAA", "mid.BBB", "mid.aaa"}


def test_the_database_refuses_a_second_inbound_row_with_the_same_mid(client, part, worker):
    request = _request(part, key="n" * 32)
    bind(client, worker, request)
    say(client, worker, "Первое", mid="mid.unique")
    conversation = MaxConversation.objects.get(request=request)
    with pytest.raises(IntegrityError), transaction.atomic():
        MaxMessage.objects.create(
            conversation=conversation,
            direction=MaxMessage.Direction.CUSTOMER,
            text="Подмена",
            external_message_id="mid.unique",
        )


def test_a_long_valid_mid_is_kept_whole_and_an_overlong_one_is_refused(client, part, worker):
    request = _request(part, key="v" * 32)
    bind(client, worker, request)
    long_mid = "mid." + "a" * 500
    say(client, worker, "длинный", mid=long_mid)
    say(client, worker, "слишком длинный", mid="mid." + "b" * 600)

    assert MaxMessage.objects.get(text="длинный").external_message_id == long_mid
    assert not MaxMessage.objects.filter(text="слишком длинный").exists()


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL concurrency integration test"
)
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_redeliveries_of_one_mid_store_one_message_and_one_ack(part):
    request = _request(part, key="j" * 32)
    token = issue_max_link(request_id=request.pk).token
    with transaction.atomic():
        max_bot.handle_update(bot_started(CUSTOMER, CUSTOMER_CHAT, token))
    update = message_created(CUSTOMER, CUSTOMER_CHAT, "Одновременно", mid="mid.race")
    barrier = threading.Barrier(4)
    errors = []

    def redeliver():
        barrier.wait()
        try:
            with transaction.atomic():
                max_bot.handle_update(update)
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=redeliver) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert customer_messages(request) == ["Одновременно"]
    assert MaxMessage.objects.filter(dedupe_key__startswith="ack:").count() == 1
    assert MaxOutboxEvent.objects.filter(kind="customer_message").count() == 1


def test_first_message_is_acknowledged_once_and_later_ones_are_not(
    client, part, worker, server
):
    request = _request(part, key="a" * 32)
    bind(client, worker, request)

    say(client, worker, "Первый вопрос")
    say(client, worker, "Второй вопрос")
    say(client, worker, "Третий вопрос")

    texts = server.texts_to(CUSTOMER_CHAT)
    assert texts.count(messaging.CUSTOMER_ACK_TEXT) == 1
    assert texts[-1] == messaging.CUSTOMER_ACK_TEXT
    assert customer_messages(request) == ["Первый вопрос", "Второй вопрос", "Третий вопрос"]


def test_summary_quotes_request_time_prices_and_the_total(client, part, worker, server):
    request = _request(part, key="p" * 32)
    PartType.objects.filter(pk=part.pk).update(recommended_price=Decimal("99999"))
    bind(client, worker, request)

    summary = server.texts_to(CUSTOMER_CHAT)[0]
    assert "Ваш заказ:" in summary
    assert "417300571 — РЕМЕНЬ ВАРИАТОРА" in summary
    assert "2 шт. × 12 500 ₽ = 25 000 ₽" in summary
    assert "Итого: 25 000 ₽" in summary
    assert "99 999" not in summary
    assert summary.endswith(messaging.CLOSING_TEXT)
    assert summary == max_service.request_summary_messages(request)[0]


def test_unknown_price_is_never_a_zero_total(client, part, worker, server):
    request = _request(part, key="z" * 32, price=False)
    bind(client, worker, request)

    summary = server.texts_to(CUSTOMER_CHAT)[0]
    assert messaging.UNKNOWN_PRICE_TEXT in summary
    assert "Итого: 0" not in summary
    assert "Есть позиции, цена которых уточняется." in summary


def test_a_long_summary_spans_messages_in_order_without_splitting_a_line(
    client, part, worker, server, monkeypatch
):
    from apps.catalog.models import Category, Manufacturer, Unit

    parts = [part]
    for number in range(11):
        extra = PartType.objects.create(
            name=f"ДЕТАЛЬ {number:02d} " + "Д" * 60,
            category=Category.objects.get(name="Двигатель"),
            unit=Unit.objects.get(name="Штука"),
            manufacturer=Manufacturer.objects.get(name="BRP"),
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal("100"),
        )
        PartNumber.objects.create(part=extra, value=f"ART-{number:02d}", is_primary=True)
        parts.append(extra)
    request = _request(
        part,
        key="L" * 32,
        lines=[RequestLineInput(part_id=p.pk, quantity="1", supply_inquiry=True) for p in parts],
    )
    monkeypatch.setattr(max_service, "MAX_TEXT_CHARS", 400)

    bind(client, worker, request)

    texts = server.texts_to(CUSTOMER_CHAT)
    assert len(texts) > 2 and all(len(text) <= 400 for text in texts)
    assert texts[1].startswith(messaging.CONTINUATION_HEADING)
    joined = "\n".join(texts)
    for p in parts[1:]:
        assert joined.count(p.name) == 1
    positions = [joined.index(p.name) for p in parts[1:]]
    assert positions == sorted(positions)


def test_customer_messages_reach_every_operator_through_the_operators_bot(
    client, part, worker, operators, operator_bot
):
    bot, tg = operator_bot
    request = _request(part, key="r" * 32)
    bind(client, worker, request)
    say(client, worker, "Можно доставку?")
    bot.iterate(poll_timeout=0)
    bot.iterate(poll_timeout=0)

    for operator in operators:
        texts = tg.texts_to(operator.telegram_user_id)
        assert any("НОВАЯ ЗАЯВКА" in text and "Связь: MAX" in text for text in texts)
        assert any(f"Клиент подключил MAX к заявке {request.reference}" in t for t in texts)
        message = tg.last_with(operator.telegram_user_id, "сообщение клиента")
        assert "Можно доставку?" in message["text"]
        assert message["reply_markup"]["inline_keyboard"][0][0]["url"].endswith(
            reverse("customer_request_detail", args=[request.pk])
        )
    assert MaxOperatorDelivery.objects.exclude(status="sent").count() == 0
    # Re-running both workers notifies nobody twice.
    before = len(tg.sent)
    drain(worker)
    bot.iterate(poll_timeout=0)
    assert len(tg.sent) == before


def test_operator_reply_from_denisstock_reaches_the_customer_once_with_audit(
    client, part, worker, server, operators, operator_bot
):
    bot, tg = operator_bot
    request = _request(part, key="y" * 32)
    bind(client, worker, request)
    say(client, worker, "Есть в наличии?")
    denis, masha = operators
    client.force_login(denis.user)
    page = client.get(reverse("customer_request_detail", args=[request.pk])).content.decode()
    assert "data-max-reply" in page and "Есть в наличии?" in page
    key = "0123456789abcdef0123456789abcdef"

    for _ in range(2):  # a double click
        response = client.post(
            reverse("customer_request_max_reply", args=[request.pk]),
            {"text": "Да, есть. Ждём вас.", "submission_key": key},
        )
        assert response.status_code == 302
    drain(worker)
    bot.iterate(poll_timeout=0)

    reply = MaxMessage.objects.get(direction=MaxMessage.Direction.OPERATOR)
    assert reply.operator_user == denis.user
    assert reply.delivery_status == MaxDeliveryStatus.SENT
    assert reply.external_message_id.startswith("mid.sent")
    assert server.texts_to(CUSTOMER_CHAT).count("Да, есть. Ждём вас.") == 1
    # The customer sees the bot's words only: no employee name or account.
    customer_view = "\n".join(server.texts_to(CUSTOMER_CHAT))
    for secret in ("denis", "Denis", str(OPERATOR_A_TG)):
        assert secret not in customer_view
    # The other operator hears about it, the author does not.
    assert any("ответ клиенту отправлен" in t for t in tg.texts_to(masha.telegram_user_id))
    assert not any("ответ клиенту отправлен" in t for t in tg.texts_to(denis.telegram_user_id))


def test_operator_reply_is_refused_without_rights_link_or_consent(
    client, part, worker, django_user_model, operators
):
    request = _request(part, key="i" * 32)
    stranger = django_user_model.objects.create_user(username="guest", password="x" * 12)
    key = "f" * 32
    with pytest.raises(max_service.OperatorReplyError):
        max_service.submit_operator_reply(
            request_id=request.pk, user=stranger, text="x", submission_key=key
        )
    with pytest.raises(max_service.OperatorReplyError, match="не подключил"):
        max_service.submit_operator_reply(
            request_id=request.pk, user=operators[0].user, text="x", submission_key=key
        )
    bind(client, worker, request)
    withdraw_consent(request_id=request.pk)
    with pytest.raises(max_service.OperatorReplyError, match="отозвал"):
        max_service.submit_operator_reply(
            request_id=request.pk, user=operators[0].user, text="x", submission_key=key
        )
    client.force_login(stranger)
    response = client.post(
        reverse("customer_request_max_reply", args=[request.pk]),
        {"text": "x", "submission_key": key},
    )
    assert response.status_code == 403
    assert not MaxMessage.objects.filter(direction="operator_to_customer").exists()


def test_operator_reply_with_nobody_else_to_tell_completes_without_deliveries(
    client, part, worker, django_user_model, operator_bot
):
    bot, tg = operator_bot
    only = _operator(django_user_model, OPERATOR_A_TG, username="solo")
    request = _request(part, key="0" * 32)
    bind(client, worker, request)
    max_service.submit_operator_reply(
        request_id=request.pk, user=only.user, text="Ответ", submission_key="1" * 32
    )
    drain(worker)
    bot.iterate(poll_timeout=0)

    event = MaxOutboxEvent.objects.get(kind="operator_reply")
    assert event.status == MaxOutboxEvent.Status.DISPATCHED
    assert event.deliveries.count() == 0
    assert event.attempts == 0
    assert not worker.has_due_work()


def test_an_event_nobody_can_receive_yet_waits_and_then_expires(client, part, worker):
    request = _request(part, key="2" * 32)
    drain(worker)
    event = MaxOutboxEvent.objects.get(request=request, kind="new_request")
    assert event.status == "pending" and event.next_attempt_at > timezone.now()
    MaxOutboxEvent.objects.update(
        created_at=timezone.now() - timedelta(days=8), next_attempt_at=timezone.now()
    )
    worker.dispatch_events()
    assert MaxOutboxEvent.objects.get(pk=event.pk).status == "expired"


def test_requests_sent_before_max_went_live_are_not_announced(part, worker):
    old = _request(part, key="3" * 32)
    CustomerRequest.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
    new = _request(part, key="4" * 32)
    drain(worker)
    assert not MaxOutboxEvent.objects.filter(request=old).exists()
    assert MaxOutboxEvent.objects.filter(request=new, kind="new_request").count() == 1


def test_an_ambiguous_send_becomes_uncertain_and_is_never_resent(client, part, db, server):
    api = MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=0.3)
    worker = MaxBotWorker(api, worker_id="max-u", heartbeat_file="",
                          pacer=SendPacer(sleep=lambda s: None))
    worker.start()
    request = _request(part, key="5" * 32)
    token = issue_max_link(request_id=request.pk).token
    server.script("/messages", ("accept_then_hang", 1.0))
    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))

    worker.iterate()
    row = MaxMessage.objects.get(dedupe_key__startswith="summary:")
    assert row.delivery_status == MaxDeliveryStatus.UNCERTAIN
    for _ in range(3):
        worker.iterate()
    assert len(server.texts_to(CUSTOMER_CHAT)) == 1  # MAX got it once; never again
    assert MaxMessage.objects.get(pk=row.pk).attempts == 1


def test_a_send_interrupted_by_a_crash_is_uncertain_after_restart(client, part, worker, server):
    request = _request(part, key="6" * 32)
    token = issue_max_link(request_id=request.pk).token
    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))
    MaxMessage.objects.update(delivery_status=MaxDeliveryStatus.SENDING)
    worker.release()

    restarted = MaxBotWorker(worker.api, worker_id="max-b", heartbeat_file="",
                             pacer=SendPacer(sleep=lambda s: None))
    restarted.start()
    restarted.iterate()

    assert MaxMessage.objects.get().delivery_status == MaxDeliveryStatus.UNCERTAIN
    assert server.texts_to(CUSTOMER_CHAT) == []


# --- Worker: retry, rate limits, ordering, single instance ---------------------------------


def test_429_is_retried_after_the_advised_pause_without_a_hot_loop(
    client, part, worker, server, sleeps
):
    request = _request(part, key="7" * 32)
    server.script(
        "/messages",
        ("status", 429, {"code": "too.many.requests", "message": "x"}, {"Retry-After": "30"}),
    )
    token = issue_max_link(request_id=request.pk).token
    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))

    for _ in range(5):
        worker.iterate()
    row = MaxMessage.objects.get(dedupe_key__startswith="summary:")
    assert row.delivery_status == MaxDeliveryStatus.PENDING
    assert row.attempts == 1
    assert row.next_attempt_at >= timezone.now() + timedelta(seconds=25)
    assert "429" in row.last_error
    posted = [r for r in server.requests if r["path"] == "/messages"]
    assert len(posted) == 1  # no hot loop while the pause lasts

    MaxMessage.objects.update(next_attempt_at=timezone.now())
    worker.iterate()
    assert MaxMessage.objects.get(pk=row.pk).delivery_status == MaxDeliveryStatus.SENT
    assert any(pause >= 25 for pause in sleeps)  # the pacer honoured MAX's pause


@pytest.mark.parametrize("status", [500, 503])
def test_server_errors_retry_with_bounded_backoff_until_failed(
    client, part, worker, server, status
):
    request = _request(part, key="8" * 32)
    token = issue_max_link(request_id=request.pk).token
    server.script("/messages", *[("status", status, {"code": "x", "message": "y"})] * MAX_ATTEMPTS)
    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))

    delays = []
    for _ in range(MAX_ATTEMPTS):
        MaxMessage.objects.update(next_attempt_at=timezone.now())
        before = timezone.now()
        worker.send_customer_messages()
        row = MaxMessage.objects.get()
        if row.delivery_status == MaxDeliveryStatus.PENDING:
            delays.append((row.next_attempt_at - before).total_seconds())
    row = MaxMessage.objects.get()
    assert row.delivery_status == MaxDeliveryStatus.FAILED
    assert row.attempts == MAX_ATTEMPTS
    assert delays == sorted(delays) and max(delays) <= 901
    assert max_bot.backoff_seconds(100) == 900


def test_a_refused_message_fails_at_once_and_is_visible(client, part, worker, server):
    request = _request(part, key="9" * 32)
    token = issue_max_link(request_id=request.pk).token
    server.script("/messages", ("status", 400, {"code": "proto.payload", "message": "bad"}))
    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))
    worker.iterate()
    row = MaxMessage.objects.get()
    assert (row.delivery_status, row.attempts) == (MaxDeliveryStatus.FAILED, 1)


def test_a_rejected_token_stops_the_worker_and_loses_nothing(client, part, worker, server):
    request = _request(part, key="T" * 32)
    token = issue_max_link(request_id=request.pk).token
    server.script("/messages", ("status", 401, {"code": "verify.token", "message": "x"}))
    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token))
    with pytest.raises(max_bot.SingleInstanceError):
        worker.send_customer_messages()
    row = MaxMessage.objects.get()
    assert (row.delivery_status, row.attempts) == (MaxDeliveryStatus.PENDING, 0)


def test_a_later_message_never_overtakes_one_waiting_to_retry(client, part, worker, server):
    request = _request(part, key="Q" * 32)
    bind(client, worker, request)
    server.script("/messages", ("status", 503, {"code": "x", "message": "busy"}))
    say(client, worker, "Первое")  # the acknowledgement meets the outage
    max_service.submit_operator_reply(
        request_id=request.pk,
        user=_seller(),
        text="Ответ после подтверждения",
        submission_key="a1" * 16,
    )
    worker.iterate()
    worker.iterate()

    ack = MaxMessage.objects.get(dedupe_key__startswith="ack:")
    reply = MaxMessage.objects.get(direction="operator_to_customer")
    assert ack.delivery_status == MaxDeliveryStatus.PENDING
    assert reply.delivery_status == MaxDeliveryStatus.PENDING
    # The held reply waits with the acknowledgement instead of staying due:
    # a due row nobody may send would spin the worker loop without a pause.
    assert reply.next_attempt_at == ack.next_attempt_at
    assert not worker.has_due_work()
    # The retry time arrives for both.
    MaxMessage.objects.filter(pk__in=[ack.pk, reply.pk]).update(next_attempt_at=timezone.now())
    worker.iterate()
    texts = server.texts_to(CUSTOMER_CHAT)
    assert texts[-2:] == [messaging.CUSTOMER_ACK_TEXT, "Ответ после подтверждения"]


def _seller():
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username="seller-q", password="x" * 12)
    user.groups.add(Group.objects.get(name=roles.SELLER))
    return user


def test_pacer_keeps_two_messages_a_second_per_dialog_and_a_global_rate():
    clock = [100.0]
    slept = []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock[0] += seconds

    pacer = SendPacer(clock=lambda: clock[0], sleep=sleep)
    pacer.wait(1)
    pacer.wait(1)
    assert slept == [max_bot.DIALOG_INTERVAL_SECONDS]
    pacer.wait(2)
    assert slept[-1] == round(max_bot.GLOBAL_INTERVAL_SECONDS, 3)
    stamps = []
    for _ in range(5):
        pacer.wait(1)
        stamps.append(clock[0])
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(gap >= max_bot.DIALOG_INTERVAL_SECONDS - 1e-9 for gap in gaps)
    assert 1 / max_bot.DIALOG_INTERVAL_SECONDS <= 2
    assert 1 / max_bot.GLOBAL_INTERVAL_SECONDS <= 30
    pacer.pause(10_000)
    before = clock[0]
    pacer.wait(3)
    assert clock[0] - before <= max_bot.RATE_LIMIT_PAUSE_MAX_SECONDS + 1e-9


def test_only_one_worker_holds_the_lease(worker, server):
    second = MaxBotWorker(worker.api, worker_id="max-z", heartbeat_file="")
    with pytest.raises(max_bot.SingleInstanceError):
        second.acquire()
    worker.release()
    second.acquire()
    with pytest.raises(max_bot.SingleInstanceError):
        worker.renew()


def test_startup_waits_out_an_outage_and_refuses_a_bad_token(db, server):
    bad = MaxBotWorker(
        MaxBotApi("fake-rejected-token", base_url=server.base_url, timeout=2),
        worker_id="max-bad", heartbeat_file="",
    )
    with pytest.raises(max_bot.SingleInstanceError):
        bad.start_with_retry()
    bad.release()

    stop = threading.Event()
    waits = []
    flaky = MaxBotWorker(
        MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=2),
        worker_id="max-flaky", heartbeat_file="", stop=stop,
    )
    flaky._wait_holding_lease = waits.append
    server.script("/me", ("status", 503, {"code": "x", "message": "down"}))
    assert flaky.start_with_retry() is True
    assert waits == [max_bot.startup_backoff_seconds(1)]


# --- Returning customer: 40-45 -----------------------------------------------------------


def test_returning_customer_selects_between_requests_and_messages_never_cross(
    client, part, worker, server, admin_user
):
    request_a = _request(part, key="A" * 32)
    bind(client, worker, request_a)  # 40: first request, first start
    say(client, worker, "Про заявку A")

    # 41: the same MAX user, a second request, no second account. The deep link
    # into an existing dialog arrives as a message with the start command.
    request_b = _request(part, key="B" * 32)
    token_b = issue_max_link(request_id=request_b.pk).token
    say(client, worker, f"/start {token_b}")
    conversation_a = MaxConversation.objects.get(request=request_a)
    conversation_b = MaxConversation.objects.get(request=request_b)
    assert conversation_b.customer_user_id == CUSTOMER
    assert server.texts_to(CUSTOMER_CHAT)[-1].startswith(
        f"Готово. MAX подключён к заявке {request_b.reference}."
    )
    say(client, worker, "Про заявку B")
    assert customer_messages(request_b) == ["Про заявку B"]

    # 42: no deterministic selection (the active request was erased) -> never guess.
    request_c = _request(part, key="C" * 32)
    say(client, worker, f"/start {issue_max_link(request_id=request_c.pk).token}")
    withdraw_consent(request_id=request_c.pk)
    anonymize_request(request_id=request_c.pk, by=admin_user)
    assert MaxCustomerChat.objects.get(user_id=CUSTOMER).active_conversation is None
    before = MaxMessage.objects.filter(direction="customer_to_operator").count()
    say(client, worker, "Куда это уйдёт?")
    assert MaxMessage.objects.filter(direction="customer_to_operator").count() == before
    prompt = server.sent[-1]
    assert prompt["text"] == max_service.AMBIGUOUS_TEXT
    buttons = prompt["attachments"][0]["payload"]["buttons"]
    payloads = {row[0]["payload"] for row in buttons}
    assert payloads == {f"s:{conversation_a.public_id.hex}", f"s:{conversation_b.public_id.hex}"}
    for row in buttons:
        # An opaque selector, never a database key or business data.
        assert re.fullmatch(r"s:[0-9a-f]{32}", row[0]["payload"])

    # 43: explicit selection of B.
    press(client, worker, conversation_b)
    assert server.texts_to(CUSTOMER_CHAT)[-1] == max_service.SELECTED_TEXT.format(
        reference=request_b.reference
    )
    assert server.answers and server.answers[-1]["body"] == {"notification": "Готово"}
    say(client, worker, "Только для B")
    assert customer_messages(request_b) == ["Про заявку B", "Только для B"]
    assert customer_messages(request_a) == ["Про заявку A"]

    # 44: switch to A.
    press(client, worker, conversation_a)
    say(client, worker, "Только для A")
    assert customer_messages(request_a) == ["Про заявку A", "Только для A"]
    assert customer_messages(request_b) == ["Про заявку B", "Только для B"]

    # 45: another user can neither select nor write into these requests.
    other_request = _request(part, key="D" * 32)
    bind(client, worker, other_request, user=OTHER, chat=OTHER_CHAT)
    press(client, worker, conversation_a, user=OTHER, chat=OTHER_CHAT)
    assert server.texts_to(OTHER_CHAT)[-1] == max_service.SELECTION_UNAVAILABLE_TEXT
    say(client, worker, "Чужое?", user=OTHER, chat=OTHER_CHAT)
    assert customer_messages(other_request) == ["Чужое?"]
    assert "Чужое?" not in customer_messages(request_a) + customer_messages(request_b)
    assert MaxCustomerChat.objects.get(user_id=CUSTOMER).active_conversation == conversation_a


def test_requests_command_and_repeat_start_offer_the_selector(client, part, worker, server):
    request_a = _request(part, key="E" * 32)
    request_b = _request(part, key="F" * 32)
    bind(client, worker, request_a)
    say(client, worker, f"/start {issue_max_link(request_id=request_b.pk).token}")

    say(client, worker, "/requests")
    selector = server.sent[-1]
    assert selector["text"].startswith(max_service.SELECT_TEXT)
    assert f"Сейчас выбрана заявка {request_b.reference}" in selector["text"]
    assert len(selector["attachments"][0]["payload"]["buttons"]) == 2

    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT))  # started again after a stop
    drain(worker)
    assert server.sent[-1]["attachments"]


def test_a_forged_callback_payload_selects_nothing(client, part, worker, server):
    request = _request(part, key="G" * 32)
    bind(client, worker, request)
    conversation = MaxConversation.objects.get(request=request)
    for payload in (
        "s:" + "0" * 32,
        f"s:{conversation.public_id.hex.upper()}",
        f"s:{request.pk}",
        "x:" + conversation.public_id.hex,
        "s:" + "../" * 20,
    ):
        deliver(client, message_callback(CUSTOMER, CUSTOMER_CHAT, payload))
    drain(worker)
    assert server.texts_to(CUSTOMER_CHAT)[-5:] == [max_service.SELECTION_UNAVAILABLE_TEXT] * 5


def test_a_redelivered_callback_confirms_once(client, part, worker, server):
    request = _request(part, key="H" * 32)
    bind(client, worker, request)
    conversation = MaxConversation.objects.get(request=request)
    update = message_callback(CUSTOMER, CUSTOMER_CHAT, f"s:{conversation.public_id.hex}")
    for _ in range(3):
        deliver(client, update)
    drain(worker)
    selected = max_service.SELECTED_TEXT.format(reference=request.reference)
    assert server.texts_to(CUSTOMER_CHAT).count(selected) == 1


def test_presses_on_one_keyboard_are_distinct_even_with_a_shared_callback_id(
    client, part, worker, server
):
    """MAX documents callback_id as the keyboard's identifier, not the press's.

    Found in the live local run: keyed by callback_id alone, the second press
    on the same selector looked like a redelivery of the first, the selection
    never moved and the next message went to the previous request.
    """
    request_a = _request(part, key="S" * 32)
    request_b = _request(part, key="U" * 32)
    bind(client, worker, request_a)
    say(client, worker, f"/start {issue_max_link(request_id=request_b.pk).token}")
    conversation_a = MaxConversation.objects.get(request=request_a)
    conversation_b = MaxConversation.objects.get(request=request_b)
    keyboard = "cb.keyboard-1"

    press_a = message_callback(
        CUSTOMER, CUSTOMER_CHAT, f"s:{conversation_a.public_id.hex}", callback_id=keyboard
    )
    for _ in range(2):  # a redelivery of the very same press
        deliver(client, press_a)
    drain(worker)
    say(client, worker, "Для A")
    deliver(
        client,
        message_callback(
            CUSTOMER, CUSTOMER_CHAT, f"s:{conversation_b.public_id.hex}", callback_id=keyboard
        ),
    )
    drain(worker)
    say(client, worker, "Для B")
    deliver(
        client,
        message_callback(
            OTHER, OTHER_CHAT, f"s:{conversation_a.public_id.hex}", callback_id=keyboard
        ),
    )
    drain(worker)

    assert customer_messages(request_a) == ["Для A"]
    assert customer_messages(request_b) == ["Для B"]
    texts = server.texts_to(CUSTOMER_CHAT)
    assert texts.count(max_service.SELECTED_TEXT.format(reference=request_a.reference)) == 1
    assert texts.count(max_service.SELECTED_TEXT.format(reference=request_b.reference)) == 1
    assert server.texts_to(OTHER_CHAT) == [max_service.SELECTION_UNAVAILABLE_TEXT]
    assert MaxCustomerChat.objects.get(user_id=CUSTOMER).active_conversation == conversation_b


def test_a_callback_whose_keyboard_message_was_deleted_still_works(client, part, worker, server):
    request = _request(part, key="I" * 32)
    bind(client, worker, request)
    conversation = MaxConversation.objects.get(request=request)
    deliver(client, message_callback(CUSTOMER, None, f"s:{conversation.public_id.hex}"))
    drain(worker)
    assert server.texts_to(CUSTOMER_CHAT)[-1].startswith("Выбрана заявка")


def test_media_is_answered_with_a_hint_and_not_stored(client, part, worker, server):
    request = _request(part, key="J" * 32)
    bind(client, worker, request)
    say(client, worker, None)
    assert server.texts_to(CUSTOMER_CHAT)[-1] == max_service.MEDIA_NOT_SUPPORTED_TEXT
    assert customer_messages(request) == []


def test_unlinked_people_get_the_greeting_and_nothing_is_stored(client, db, worker, server):
    say(client, worker, "Привет", user=OTHER, chat=OTHER_CHAT)
    assert server.texts_to(OTHER_CHAT) == [max_service.UNLINKED_GREETING]
    assert not MaxMessage.objects.filter(direction="customer_to_operator").exists()


# --- Cancellation, privacy, history ---------------------------------------------------------


def test_cancellation_keeps_history_and_never_rebinds(
    client, part, worker, server, admin_user
):
    request = _request(part, key="K" * 32)
    bind(client, worker, request)
    say(client, worker, "Передумал")
    pending_token = issue_max_link(request_id=request.pk).token

    change_request_status(request_id=request.pk, target_status="canceled", by=admin_user)
    deliver(client, bot_started(OTHER, OTHER_CHAT, pending_token))
    drain(worker)

    conversation = MaxConversation.objects.get(request=request)
    assert conversation.customer_user_id == CUSTOMER
    assert customer_messages(request) == ["Передумал"]
    assert MaxMessage.objects.filter(conversation=conversation).count() >= 3
    assert server.texts_to(OTHER_CHAT) == [max_service.LINK_INVALID_TEXT]
    with pytest.raises(MessengerLinkError):
        issue_max_link(request_id=request.pk)
    client.force_login(admin_user)
    page = client.get(reverse("customer_request_detail", args=[request.pk])).content.decode()
    assert "Передумал" in page and "data-max-history" in page


def test_withdrawn_consent_closes_the_conversation_and_stops_queued_sends(
    client, part, worker, server
):
    request = _request(part, key="M" * 32)
    bind(client, worker, request)
    say(client, worker, "Первое")
    server.script("/messages", ("status", 503, {"code": "x", "message": "y"}))
    max_service.submit_operator_reply(
        request_id=request.pk, user=_seller(), text="Ответ в очереди", submission_key="b2" * 16
    )
    worker.iterate()
    reply = MaxMessage.objects.get(direction="operator_to_customer")
    assert reply.delivery_status == MaxDeliveryStatus.PENDING

    withdraw_consent(request_id=request.pk)
    MaxMessage.objects.filter(pk=reply.pk).update(next_attempt_at=timezone.now())
    say(client, worker, "Ещё одно")

    assert MaxMessage.objects.get(pk=reply.pk).delivery_status == MaxDeliveryStatus.FAILED
    assert "Ответ в очереди" not in server.texts_to(CUSTOMER_CHAT)
    assert customer_messages(request) == ["Первое"]
    assert server.texts_to(CUSTOMER_CHAT)[-1] == max_service.CLOSED_TEXT


def test_anonymization_erases_texts_identity_and_routing(client, part, worker, admin_user):
    request = _request(part, key="N" * 32)
    bind(client, worker, request)
    say(client, worker, "Мой адрес: улица Ленина")
    withdraw_consent(request_id=request.pk)
    anonymize_request(request_id=request.pk, by=admin_user)

    conversation = MaxConversation.objects.get(request=request)
    assert conversation.status == "closed"
    assert conversation.customer_user_id is None and conversation.customer_chat_id is None
    texts = MaxMessage.objects.filter(conversation=conversation).values_list("text", flat=True)
    assert set(texts) == {""}
    assert not MaxCustomerChat.objects.filter(user_id=CUSTOMER).exists()
    assert not request.__class__.objects.get(pk=request.pk).messenger_link_tokens.filter(
        used_at__isnull=True, revoked_at__isnull=True
    ).exists()


def test_max_rows_never_touch_telegram_tables_or_stock(client, part, worker):
    from apps.inventory.models import StockBalance, StockMovement

    before = (StockMovement.objects.count(), StockBalance.objects.count())
    request = _request(part, key="P" * 32)
    bind(client, worker, request)
    say(client, worker, "Вопрос")
    assert not TelegramConversation.objects.exists()
    assert (StockMovement.objects.count(), StockBalance.objects.count()) == before


# --- Stage 0: a closed request never takes customer messages -------------------------------


def _customer_message_events():
    return MaxOutboxEvent.objects.filter(kind=MaxOutboxEvent.Kind.CUSTOMER_MESSAGE).count()


def test_a_cancelled_current_request_refuses_the_message_and_offers_the_open_ones(
    client, part, worker, server, admin_user
):
    request_a = _request(part, key="sa" * 16)
    request_b = _request(part, key="sb" * 16)
    bind(client, worker, request_a)
    say(client, worker, "Про A")
    bind(client, worker, request_b)
    say(client, worker, "Про B")
    conversation_a = MaxConversation.objects.get(request=request_a)
    conversation_b = MaxConversation.objects.get(request=request_b)
    history_b = MaxMessage.objects.filter(conversation=conversation_b).count()
    events = _customer_message_events()

    change_request_status(request_id=request_b.pk, target_status="canceled", by=admin_user)
    say(client, worker, "Ещё про B")

    assert customer_messages(request_b) == ["Про B"]
    assert customer_messages(request_a) == ["Про A"]  # never silently moved to A
    assert _customer_message_events() == events  # nobody is notified
    prompt = server.sent[-1]
    assert prompt["text"] == messaging.closed_request_text(request_b.reference, other_open=True)
    buttons = prompt["attachments"][0]["payload"]["buttons"]
    assert {row[0]["payload"] for row in buttons} == {f"s:{conversation_a.public_id.hex}"}
    # The customer's choice is left as it was, and nothing of B's history is lost.
    assert MaxCustomerChat.objects.get(user_id=CUSTOMER).active_conversation == conversation_b
    assert MaxMessage.objects.filter(conversation=conversation_b).count() >= history_b
    request_b.refresh_from_db()
    assert request_b.status == "canceled"

    press(client, worker, conversation_a)
    say(client, worker, "Теперь про A")
    assert customer_messages(request_a) == ["Про A", "Теперь про A"]
    assert customer_messages(request_b) == ["Про B"]


def test_a_completed_only_request_refuses_messages_and_offers_nothing(
    client, part, worker, server, admin_user
):
    request = _request(part, key="sc" * 16)
    bind(client, worker, request)
    say(client, worker, "Спасибо")
    change_request_status(request_id=request.pk, target_status="in_progress", by=admin_user)
    change_request_status(request_id=request.pk, target_status="completed", by=admin_user)
    events = _customer_message_events()

    say(client, worker, "А ещё вопрос")

    assert customer_messages(request) == ["Спасибо"]
    assert _customer_message_events() == events
    prompt = server.sent[-1]
    assert prompt["text"] == messaging.closed_request_text(request.reference, other_open=False)
    assert not prompt.get("attachments")


def test_requests_hide_closed_requests_and_a_stale_button_changes_nothing(
    client, part, worker, server, admin_user
):
    request_a = _request(part, key="sd" * 16)
    request_b = _request(part, key="se" * 16)
    bind(client, worker, request_a)
    bind(client, worker, request_b)
    conversation_a = MaxConversation.objects.get(request=request_a)
    conversation_b = MaxConversation.objects.get(request=request_b)
    change_request_status(request_id=request_a.pk, target_status="canceled", by=admin_user)

    say(client, worker, "/requests")
    buttons = server.sent[-1]["attachments"][0]["payload"]["buttons"]
    assert {row[0]["payload"] for row in buttons} == {f"s:{conversation_b.public_id.hex}"}

    stale = message_callback(CUSTOMER, CUSTOMER_CHAT, f"s:{conversation_a.public_id.hex}")
    assert deliver(client, stale).status_code == 200
    assert deliver(client, stale).status_code == 200  # MAX delivers the same press again
    drain(worker)

    closed_text = messaging.closed_request_text(request_a.reference, other_open=True)
    assert server.texts_to(CUSTOMER_CHAT).count(closed_text) == 1
    assert MaxCustomerChat.objects.get(user_id=CUSTOMER).active_conversation == conversation_b
    say(client, worker, "Про B")
    assert customer_messages(request_b) == ["Про B"]
    assert customer_messages(request_a) == []


def test_a_completed_request_can_neither_issue_nor_consume_a_max_link(
    client, part, worker, server, admin_user
):
    request = _request(part, key="sf" * 16)
    token = issue_max_link(request_id=request.pk).token
    change_request_status(request_id=request.pk, target_status="in_progress", by=admin_user)
    change_request_status(request_id=request.pk, target_status="completed", by=admin_user)

    with pytest.raises(MessengerLinkError):
        issue_max_link(request_id=request.pk)
    assert deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, token)).status_code == 200
    drain(worker)

    assert not MaxConversation.objects.filter(
        request=request, status=MaxConversation.Status.LINKED
    ).exists()
    assert server.texts_to(CUSTOMER_CHAT) == [max_service.LINK_INVALID_TEXT]
