"""Telegram messaging for customer requests: bot worker, outbox and security.

The Bot API is replaced by ``FakeBotApi``; everything else (models, services,
worker loop, update handling) is the production code. No network is used and
no real token exists anywhere in these tests.
"""

import itertools
import logging
import re
import threading
import urllib.error
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, connections
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import PartNumber, PartType
from apps.core.observability import RedactingFormatter, redact
from apps.customer_requests import messaging
from apps.customer_requests import telegram_service as service_module
from apps.customer_requests.messengers import (
    MessengerLinkError,
    consume_telegram_start,
    issue_telegram_link,
)
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestLine,
    CustomerRequestMessengerContact,
    TelegramConversation,
    TelegramCustomerChat,
    TelegramDelivery,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramOperator,
    TelegramOutboxEvent,
)
from apps.customer_requests.services import (
    RequestLineInput,
    anonymize_request,
    change_request_status,
    create_customer_request,
    withdraw_consent,
)
from apps.customer_requests.telegram_api import (
    TelegramApiError,
    TelegramBotApi,
    TelegramNetworkError,
)
from apps.customer_requests.telegram_bot import (
    MAX_ATTEMPTS,
    SingleInstanceError,
    TelegramBotWorker,
    handle_update,
)
from apps.customer_requests.telegram_service import CUSTOMER_ACK_TEXT, request_summary_messages
from apps.inventory.models import StockBalance, StockMovement
from apps.operations.models import TelegramBotRuntime
from apps.receipts.models import Receipt
from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, Sale

from .test_customer_requests import POLICY


@pytest.fixture(autouse=True)
def messenger_cabinet_enabled(settings):
    settings.CUSTOMER_MESSENGER_CABINET_ENABLED = True

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop"
CUSTOMER = 700001
OTHER_CUSTOMER = 700002
OPERATOR_A = 800001
OPERATOR_B = 800002
STRANGER = 900001
_ids = itertools.count(10_000)


def build_part():
    """The one catalogue card these suites order; shared with the operator suite."""
    from apps.catalog.models import Category, Manufacturer, Unit

    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    result = PartType.objects.create(
        name="РЕМЕНЬ ПРИВОДНОЙ",
        category=category,
        unit=Unit.objects.get(name="Штука"),
        manufacturer=Manufacturer.objects.get_or_create(name="BRP")[0],
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("10000.00"),
        certified_price_rub=Decimal("10000.00"),
        price_provenance=PartType.PriceProvenance.FORMULA_CERTIFIED,
    )
    PartNumber.objects.create(part=result, value="448", is_primary=True)
    return result


@pytest.fixture
def part(db):
    return build_part()


class FakeBotApi:
    def __init__(self):
        self.updates = []
        self.sent = []
        self.answers = []
        self.failures = []
        self.webhook = {"url": ""}
        self.get_updates_error = None

    def get_me(self):
        return {"username": "ProStorTestBot"}

    def get_webhook_info(self):
        return self.webhook

    def get_updates(self, *, offset, timeout):
        if self.get_updates_error:
            raise self.get_updates_error
        return [update for update in self.updates if update["update_id"] >= offset]

    def send_message(self, *, chat_id, text, reply_markup=None):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, *, callback_query_id, text=""):
        self.answers.append((callback_query_id, text))

    def texts_to(self, chat_id):
        return [item["text"] for item in self.sent if item["chat_id"] == chat_id]

    def all_text(self):
        return "\n".join(item["text"] for item in self.sent)

    def last_with(self, chat_id, fragment):
        """Replies interleave with outbox notifications; find the one meant."""
        return next(
            item
            for item in reversed(self.sent)
            if item["chat_id"] == chat_id and fragment in item["text"]
        )


def message_update(user_id, text=None, *, update_id=None, chat_type="private", **extra):
    message = {
        "message_id": 1,
        "chat": {"id": user_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "username": f"user{user_id}"},
        **extra,
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id or next(_ids), "message": message}


def callback_update(user_id, data):
    number = next(_ids)
    return {
        "update_id": number,
        "callback_query": {
            "id": f"cb{number}",
            "from": {"id": user_id},
            "data": data,
            "message": {"chat": {"id": user_id, "type": "private"}},
        },
    }


def _request(part, *, key, messenger=CustomerRequest.Messenger.TELEGRAM):
    request, created = create_customer_request(
        customer_name="Иван Петров",
        customer_phone="+7 (912) 123-45-67",
        preferred_messenger=messenger,
        comment="Нужна деталь до пятницы.",
        lines=[RequestLineInput(part_id=part.pk, quantity="2", supply_inquiry=True)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=key,
    )
    assert created
    return request


def _operator(django_user_model, telegram_id, *, username, role=roles.SELLER, superuser=False):
    if superuser:
        user = django_user_model.objects.create_superuser(username=username, password="x" * 12)
    else:
        user = django_user_model.objects.create_user(username=username, password="x" * 12)
        user.groups.add(Group.objects.get(name=role))
    return TelegramOperator.objects.create(user=user, telegram_user_id=telegram_id)


@pytest.fixture
def api():
    return FakeBotApi()


@pytest.fixture
def worker(db, api):
    bot = TelegramBotWorker(api, worker_id="worker-a", poll_timeout=0, heartbeat_file="")
    bot.start()
    return bot


@pytest.fixture
def operators(db, django_user_model):
    return (
        _operator(django_user_model, OPERATOR_A, username="denis"),
        _operator(django_user_model, OPERATOR_B, username="masha"),
    )


def run(worker, api, *updates):
    api.updates.extend(updates)
    worker.iterate(poll_timeout=0)
    # A second drain proves nothing new appears on a repeated cycle.
    worker.iterate(poll_timeout=0)


def link(worker, api, request, chat_id=CUSTOMER):
    token = issue_telegram_link(request_id=request.pk).token
    run(worker, api, message_update(chat_id, f"/start {token}"))
    return token


def _business_state():
    return {
        "movements": StockMovement.objects.count(),
        "balances": StockBalance.objects.count(),
        "reservations": Reservation.objects.count(),
        "sales": Sale.objects.count(),
        "repairs": RepairOrder.objects.count(),
        "receipts": Receipt.objects.count(),
    }


# --- A, B, C, D: request creation, outage, one notification ------------------------------


def test_telegram_request_is_a_normal_request_with_waiting_conversation(part):
    request = _request(part, key="a" * 32)

    conversation = TelegramConversation.objects.get(request=request)
    assert conversation.status == TelegramConversation.Status.AWAITING_LINK
    assert conversation.customer_chat_id is None
    assert request.status == CustomerRequest.Status.NEW
    assert request.lines.count() == 1
    event = TelegramOutboxEvent.objects.get()
    assert event.kind == TelegramOutboxEvent.Kind.NEW_REQUEST
    assert event.dedupe_key == f"new_request:{request.pk}"


def test_max_request_gets_no_telegram_rows(part):
    _request(part, key="m" * 32, messenger=CustomerRequest.Messenger.MAX)
    assert not TelegramConversation.objects.exists()
    assert not TelegramOutboxEvent.objects.exists()


def test_telegram_outage_never_fails_or_rolls_back_a_request(part, worker, api, operators):
    api.failures = [TelegramNetworkError("URLError", ambiguous=False)] * 2
    api.get_updates_error = TelegramNetworkError("URLError", ambiguous=False)

    request = _request(part, key="b" * 32)
    with pytest.raises(TelegramNetworkError):
        worker.iterate(poll_timeout=0)
    api.get_updates_error = None
    worker.drain_outbox()

    assert CustomerRequest.objects.filter(pk=request.pk).exists()
    pending = TelegramDelivery.objects.filter(status=TelegramDeliveryStatus.PENDING)
    assert pending.count() == 2  # both refused sends wait for a retry
    assert all(row.last_error and row.next_attempt_at > timezone.now() for row in pending)


def test_new_request_notifies_each_operator_exactly_once(part, worker, api, operators):
    request = _request(part, key="c" * 32)

    run(worker, api)
    worker.dispatch_events()
    worker.drain_outbox()

    for operator in operators:
        cards = [t for t in api.texts_to(operator.telegram_user_id) if "НОВАЯ ЗАЯВКА" in t]
        assert len(cards) == 1
        assert request.reference in cards[0]
    assert TelegramDelivery.objects.count() == 2
    assert TelegramOutboxEvent.objects.get().status == TelegramOutboxEvent.Status.DISPATCHED


def test_event_waits_while_no_operator_exists_and_is_not_lost(part, worker, api, django_user_model):
    _request(part, key="w" * 32)
    run(worker, api)
    event = TelegramOutboxEvent.objects.get()
    assert event.status == TelegramOutboxEvent.Status.PENDING
    assert not api.sent

    operator = _operator(django_user_model, OPERATOR_A, username="late")
    TelegramOutboxEvent.objects.update(next_attempt_at=timezone.now())
    run(worker, api)
    assert len(api.texts_to(operator.telegram_user_id)) == 1


def test_operator_card_matches_the_stored_request_without_n_plus_one(part, worker, api, operators):
    request = _request(part, key="d" * 32)
    line = request.lines.get()
    # A priced in-stock line shows its total; the snapshot is authoritative.
    line.is_supply_inquiry = False
    line.save(update_fields=["is_supply_inquiry"])
    run(worker, api)

    card = api.texts_to(OPERATOR_A)[0]
    assert f"НОВАЯ ЗАЯВКА №{request.reference} · Telegram" in card
    assert "Клиент: Иван Петров" in card
    assert "Телефон: +7 912 123-45-67" in card
    assert "448 · РЕМЕНЬ ПРИВОДНОЙ" in card
    assert "2 шт" in card
    assert "10 000 ₽" in card or "10 000 ₽" in card
    assert "= 20" in card
    assert "Комментарий: Нужна деталь до пятницы." in card
    assert "ожидает подключения" in card
    # Nobody to answer yet: no [Ответить] that could only say "not linked".
    buttons = [b["text"] for row in api.sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert "Ответить" not in buttons and "Активные заявки" in buttons

    from apps.customer_requests import telegram_service as service

    for number in range(20):
        extra = PartType.objects.create(
            name=f"ДЕТАЛЬ {number}",
            category=part.category,
            unit=part.unit,
            tracking_mode=PartType.TrackingMode.BULK,
        )
        PartNumber.objects.create(part=extra, value=f"X{number}", is_primary=True)
        CustomerRequestLine.objects.create(
            request=request,
            part_type=extra,
            quantity_requested=Decimal("1"),
            unit_name="Штука",
            unit_short_name="шт",
            price_seen=Decimal("5"),
            article=f"X{number}",
            part_name=f"ДЕТАЛЬ {number}",
        )
    conversation = TelegramConversation.objects.get()
    with CaptureQueriesContext(connection) as queries:
        text = service.request_card_text(service.conversation_by_hex(conversation.public_id.hex))
    assert "ДЕТАЛЬ 19" in text
    assert len(queries) <= 2


@override_settings(TELEGRAM_INTERNAL_BASE_URL="https://denisstock.example")
def test_open_request_button_goes_to_the_internal_page_for_operators_only(
    part, worker, api, operators
):
    request = _request(part, key="u" * 32)
    link(worker, api, request)
    inline = [
        item for item in api.sent if (item["reply_markup"] or {}).get("inline_keyboard")
    ]
    buttons = [
        button
        for item in inline
        for row in item["reply_markup"]["inline_keyboard"]
        for button in row
        if "url" in button
    ]
    assert buttons
    # Only employees get request buttons; the customer gets their own keyboard.
    assert all(item["chat_id"] in {OPERATOR_A, OPERATOR_B} for item in inline)
    customer_markups = [
        item["reply_markup"] for item in api.sent if item["chat_id"] == CUSTOMER
    ]
    assert all(markup == service_module.customer_keyboard() for markup in customer_markups)
    assert buttons[0]["url"] == f"https://denisstock.example/customer-requests/{request.pk}/"
    assert "denisstock.example" not in "".join(api.texts_to(CUSTOMER))


# --- E, F, G, H, I: deep link ------------------------------------------------------------


def test_deep_link_binds_numeric_chat_and_confirms_once(part, worker, api, operators):
    request = _request(part, key="e" * 32)
    link(worker, api, request)

    conversation = TelegramConversation.objects.get(request=request)
    assert conversation.status == TelegramConversation.Status.LINKED
    assert conversation.customer_chat_id == CUSTOMER
    assert conversation.customer_user_id == CUSTOMER
    confirmations = api.texts_to(CUSTOMER)
    assert len(confirmations) == 1
    assert confirmations[0].startswith(
        f"Добрый день! Ваша заявка №{request.reference} получена."
    )
    assert "Ваш заказ:" in confirmations[0]
    assert "Итого: 20 000 ₽" in confirmations[0]
    assert "Если у Вас есть вопросы по заявке, напишите нам здесь" in confirmations[0]
    for operator in operators:
        assert any(
            f"Клиент подключил Telegram к заявке №{request.reference}" in text
            for text in api.texts_to(operator.telegram_user_id)
        )
    # Nothing about the request (phone, comment) is sent to the customer.
    assert "912" not in api.texts_to(CUSTOMER)[0]


def test_expired_link_is_rejected_without_data(part, worker, api):
    request = _request(part, key="f" * 32)
    issued = issue_telegram_link(request_id=request.pk)
    request.messenger_link_tokens.update(expires_at=timezone.now() - timedelta(seconds=1))

    run(worker, api, message_update(CUSTOMER, f"/start {issued.token}"))

    assert api.texts_to(CUSTOMER) and "недействительна" in api.texts_to(CUSTOMER)[0]
    assert not TelegramConversation.objects.get(request=request).is_linked


def test_reused_link_cannot_take_over_the_conversation(part, worker, api):
    request = _request(part, key="g" * 32)
    token = link(worker, api, request, chat_id=CUSTOMER)

    run(worker, api, message_update(OTHER_CUSTOMER, f"/start {token}"))

    conversation = TelegramConversation.objects.get(request=request)
    assert conversation.customer_chat_id == CUSTOMER
    assert "недействительна" in api.texts_to(OTHER_CUSTOMER)[0]
    assert request.reference not in "".join(api.texts_to(OTHER_CUSTOMER))


def test_second_consumer_of_one_token_is_refused_in_sequence(part):
    request = _request(part, key="h" * 32)
    token = issue_telegram_link(request_id=request.pk).token
    consume_telegram_start(token=token, chat_id=CUSTOMER, user_id=CUSTOMER)
    with pytest.raises(MessengerLinkError):
        consume_telegram_start(token=token, chat_id=OTHER_CUSTOMER, user_id=OTHER_CUSTOMER)
    assert TelegramConversation.objects.get().customer_chat_id == CUSTOMER
    assert TelegramMessage.objects.filter(direction=TelegramMessage.Direction.SYSTEM).count() == 1


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL concurrency integration test"
)
def test_concurrent_token_consumers_bind_exactly_one_chat(part):
    import threading

    from django.db import connections

    request = _request(part, key="k" * 32)
    token = issue_telegram_link(request_id=request.pk).token
    barrier = threading.Barrier(2)
    outcomes = []

    def consume(chat_id):
        barrier.wait()
        try:
            consume_telegram_start(token=token, chat_id=chat_id, user_id=chat_id)
            outcomes.append(("ok", chat_id))
        except MessengerLinkError:
            outcomes.append(("refused", chat_id))
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=consume, args=(chat,)) for chat in (CUSTOMER, OTHER_CUSTOMER)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(kind for kind, _ in outcomes) == ["ok", "refused"]
    winner = next(chat for kind, chat in outcomes if kind == "ok")
    assert TelegramConversation.objects.get().customer_chat_id == winner
    assert TelegramMessage.objects.filter(direction=TelegramMessage.Direction.SYSTEM).count() == 1


def test_customer_starts_after_a_delay_within_the_link_lifetime(part, worker, api):
    request = _request(part, key="j" * 32)
    issued = issue_telegram_link(request_id=request.pk)
    request.messenger_link_tokens.filter(used_at__isnull=True, revoked_at__isnull=True).update(
        created_at=timezone.now() - timedelta(hours=20)
    )
    run(worker, api, message_update(CUSTOMER, f"/start {issued.token}"))
    assert TelegramConversation.objects.get(request=request).is_linked


def test_start_summary_uses_immutable_request_line_snapshots(part, worker, api):
    request = _request(part, key="s" * 32)
    line = request.lines.get()
    part.recommended_price = Decimal("999999.00")
    part.save(update_fields=["recommended_price"])

    link(worker, api, request)

    summary = "\n".join(api.texts_to(CUSTOMER))
    assert request.reference in summary
    assert "448 - РЕМЕНЬ ПРИВОДНОЙ" in summary
    assert "2 шт. × 10 000 ₽ = 20 000 ₽" in summary
    assert "Итого: 20 000 ₽" in summary
    assert str(line.price_seen) not in summary  # never Decimal(...) presentation
    assert "999 999" not in summary


def test_start_summary_is_honest_for_unknown_and_mixed_prices(part, worker, api):
    request = _request(part, key="u" * 32)
    first = request.lines.get()
    CustomerRequestLine.objects.filter(pk=first.pk).update(price_seen=None)
    second = PartType.objects.create(
        name="ВТОРАЯ ДЕТАЛЬ",
        category=part.category,
        unit=part.unit,
        manufacturer=part.manufacturer,
        tracking_mode=part.tracking_mode,
        recommended_price=Decimal("45000"),
        certified_price_rub=Decimal("45000"),
        price_provenance=part.price_provenance,
    )
    CustomerRequestLine.objects.create(
        request=request,
        part_type=second,
        quantity_requested=Decimal("2"),
        unit_name=part.unit.name,
        unit_short_name=part.unit.short_name,
        price_seen=Decimal("45000"),
        article="421000667",
        part_name="ВТОРАЯ ДЕТАЛЬ",
    )

    link(worker, api, request)

    summary = "\n".join(api.texts_to(CUSTOMER))
    assert "448 - РЕМЕНЬ ПРИВОДНОЙ\n2 шт. - цена уточняется" in summary
    assert "421000667 - ВТОРАЯ ДЕТАЛЬ" in summary
    assert "2 шт. × 45 000 ₽ = 90 000 ₽" in summary
    assert "Итого по позициям с известной ценой: 90 000 ₽" in summary
    assert "Есть позиции, цена которых уточняется." in summary
    assert "Итого: 0 ₽" not in summary


def test_start_summary_without_any_known_price_has_no_zero_total(part, worker, api):
    request = _request(part, key="n" * 32)
    CustomerRequestLine.objects.filter(request=request).update(price_seen=None)

    link(worker, api, request)

    summary = "\n".join(api.texts_to(CUSTOMER))
    assert "цена уточняется" in summary
    assert "Итого:" not in summary
    assert "0 ₽" not in summary


def test_long_start_summary_splits_only_between_complete_lines(monkeypatch):
    from apps.customer_requests import telegram_service

    lines = [
        SimpleNamespace(
            article=f"A{i:02d}",
            part_name="ДЕТАЛЬ " + ("X" * 90),
            quantity_requested=Decimal("1"),
            unit_short_name="шт.",
            price_seen=Decimal("88"),
        )
        for i in range(12)
    ]
    request = SimpleNamespace(
        reference="ABCD1234", lines=SimpleNamespace(order_by=lambda *_: lines)
    )
    monkeypatch.setattr(telegram_service, "MAX_MESSAGE_CHARS", 420)

    messages = request_summary_messages(request)

    assert len(messages) > 1
    assert all(len(message) <= 420 for message in messages)
    assert all(sum(f"A{i:02d}" in message for message in messages) == 1 for i in range(12))
    assert messages[-1].endswith(
        "Если у Вас есть вопросы по заявке, напишите нам здесь - сервис PRO-STOR ответит Вам."
    )


def test_customer_who_never_starts_keeps_a_valid_request(part, worker, api, operators):
    request = _request(part, key="n" * 32)
    run(worker, api)
    request.refresh_from_db()
    assert request.status == CustomerRequest.Status.NEW
    assert TelegramConversation.objects.get().status == TelegramConversation.Status.AWAITING_LINK


# --- J, K, L, S: operator authorization --------------------------------------------------


def test_random_user_sees_customer_ux_and_whoami_shows_no_internal_onboarding(
    part, worker, api, operators
):
    request = _request(part, key="p" * 32)
    conversation = TelegramConversation.objects.get()
    hex_id = conversation.public_id.hex

    run(
        worker,
        api,
        message_update(STRANGER, "/requests"),
        message_update(STRANGER, "/start"),
        message_update(STRANGER, "/whoami"),
        callback_update(STRANGER, f"c:{hex_id}"),
        callback_update(STRANGER, f"r:{hex_id}"),
        callback_update(STRANGER, "l:1"),
    )

    stranger_text = "\n".join(api.texts_to(STRANGER))
    assert f"Ваш Telegram ID: {STRANGER}" not in stranger_text
    assert "Если вы сотрудник" not in stranger_text
    assert "Команды:" not in stranger_text
    assert "/requests" not in stranger_text
    assert "заявкам" in stranger_text.lower()
    for secret in (f"№{request.human_number}", "Иван", "912", "448", "Нужна деталь"):
        assert secret not in stranger_text
    assert [text for _id, text in api.answers] == ["Недоступно."] * 3
    assert not TelegramOperator.objects.filter(telegram_user_id=STRANGER).exists()


@pytest.mark.parametrize("revoke", ["operator_inactive", "user_inactive", "no_sales_role"])
def test_deactivated_operator_is_denied_everywhere(part, worker, api, django_user_model, revoke):
    operator = _operator(django_user_model, OPERATOR_A, username="former")
    _request(part, key="q" * 32)
    conversation = TelegramConversation.objects.get()
    if revoke == "operator_inactive":
        TelegramOperator.objects.filter(pk=operator.pk).update(is_active=False)
    elif revoke == "user_inactive":
        type(operator.user).objects.filter(pk=operator.user_id).update(is_active=False)
    else:
        operator.user.groups.clear()
        operator.user.groups.add(Group.objects.get(name=roles.VIEWER))

    run(
        worker,
        api,
        message_update(OPERATOR_A, "/requests"),
        callback_update(OPERATOR_A, f"c:{conversation.public_id.hex}"),
    )

    assert conversation.request.reference not in "\n".join(api.texts_to(OPERATOR_A))
    assert api.answers[-1][1] == "Недоступно."


def test_delivery_to_an_operator_disabled_after_dispatch_is_not_sent(part, worker, api, operators):
    _request(part, key="r" * 32)
    worker.dispatch_events()
    TelegramOperator.objects.filter(pk=operators[0].pk).update(is_active=False)
    worker.send_operator_deliveries()
    assert not api.texts_to(OPERATOR_A)
    failed = TelegramDelivery.objects.get(operator=operators[0])
    assert failed.status == TelegramDeliveryStatus.FAILED


def test_authorized_operator_lists_and_opens_requests(part, worker, api, operators):
    request = _request(part, key="s" * 32)
    conversation = TelegramConversation.objects.get()

    run(worker, api, message_update(OPERATOR_A, "/requests"))
    listing = api.last_with(OPERATOR_A, "Активные заявки")
    button = listing["reply_markup"]["inline_keyboard"][0][0]
    assert request.reference in button["text"] and "Telegram" in button["text"]
    assert button["callback_data"] == f"c:{request.public_id.hex}"
    assert conversation.public_id.hex not in button["callback_data"]
    assert re.fullmatch(r"c:[0-9a-f]{32}", button["callback_data"])  # opaque, no pk

    run(worker, api, callback_update(OPERATOR_A, button["callback_data"]))
    assert any(text.startswith(f"ЗАЯВКА №{request.reference}") for text in api.texts_to(OPERATOR_A))


@pytest.mark.parametrize("data", ["c:zzz", "c:", "r:" + "0" * 32, "l:-1", "q:1", "c:" + "A" * 32])
def test_forged_callback_data_is_refused_server_side(part, worker, api, operators, data):
    _request(part, key="t" * 32)
    run(worker, api, callback_update(OPERATOR_A, data))
    if data == "r:" + "0" * 32:
        assert "Заявка не найдена." in api.texts_to(OPERATOR_A)
    elif data == "l:-1":
        assert api.last_with(OPERATOR_A, "Активные заявки")
    else:
        assert api.answers[-1][1] == "Недоступно."
    assert not TelegramOperator.objects.exclude(reply_request=None).exists()


# --- M, N, O, P: messaging ---------------------------------------------------------------


def _reply(worker, api, conversation, text, operator_id=OPERATOR_A):
    run(worker, api, callback_update(operator_id, f"r:{conversation.public_id.hex}"))
    run(worker, api, message_update(operator_id, text))


def test_full_conversation_is_stored_delivered_and_attributed(part, worker, api, operators):
    request = _request(part, key="v" * 32)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get()

    run(worker, api, message_update(CUSTOMER, "Здравствуйте, когда можно забрать?"))
    assert any("Здравствуйте, когда можно забрать?" in text for text in api.texts_to(OPERATOR_A))
    assert any("Здравствуйте, когда можно забрать?" in text for text in api.texts_to(OPERATOR_B))

    _reply(worker, api, conversation, "Добрый день. Деталь есть, можно забрать сегодня.")

    assert api.texts_to(CUSTOMER)[-1] == "Добрый день. Деталь есть, можно забрать сегодня."
    reply = TelegramMessage.objects.get(direction=TelegramMessage.Direction.OPERATOR)
    assert reply.operator_user == operators[0].user
    assert reply.delivery_status == TelegramDeliveryStatus.SENT
    assert reply.telegram_message_id
    # The other operator sees who answered; the author gets no echo of it.
    assert any("Сотрудник: denis" in text for text in api.texts_to(OPERATOR_B))
    assert not any("Сотрудник: denis" in text for text in api.texts_to(OPERATOR_A))
    # The customer never sees the employee identity.
    assert "denis" not in "\n".join(api.texts_to(CUSTOMER))

    history = list(conversation.messages.values_list("direction", "text"))
    assert [direction for direction, _ in history] == [
        TelegramMessage.Direction.SYSTEM,
        TelegramMessage.Direction.CUSTOMER,
        TelegramMessage.Direction.OPERATOR,
    ]


def test_reply_to_unlinked_customer_is_refused_not_discarded_silently(part, worker, api, operators):
    _request(part, key="x" * 32)
    conversation = TelegramConversation.objects.get()
    run(worker, api, callback_update(OPERATOR_A, f"r:{conversation.public_id.hex}"))
    assert api.last_with(OPERATOR_A, "Клиент ещё не подключил Telegram")
    run(worker, api, message_update(OPERATOR_A, "Здравствуйте"))
    assert not TelegramMessage.objects.filter(direction=TelegramMessage.Direction.OPERATOR).exists()
    assert api.last_with(OPERATOR_A, "«Ответить»")


def test_reply_cancel_and_expired_reply_window(part, worker, api, operators):
    request = _request(part, key="y" * 32)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get()
    run(worker, api, callback_update(OPERATOR_A, f"r:{conversation.public_id.hex}"))
    run(worker, api, callback_update(OPERATOR_A, "x"))
    run(worker, api, message_update(OPERATOR_A, "не должно уйти"))
    run(worker, api, callback_update(OPERATOR_A, f"r:{conversation.public_id.hex}"))
    TelegramOperator.objects.filter(telegram_user_id=OPERATOR_A).update(
        reply_started_at=timezone.now() - timedelta(hours=1)
    )
    run(worker, api, message_update(OPERATOR_A, "тоже не должно уйти"))
    assert api.last_with(OPERATOR_A, "истёк")
    assert not TelegramMessage.objects.filter(direction=TelegramMessage.Direction.OPERATOR).exists()


def test_duplicate_update_is_stored_once(part, worker, api, operators):
    request = _request(part, key="z" * 32)
    link(worker, api, request)
    update = message_update(CUSTOMER, "Есть в наличии?")

    handle_update(update)
    handle_update(update)
    run(worker, api, update)  # the offset already passed it, too

    assert TelegramMessage.objects.filter(direction=TelegramMessage.Direction.CUSTOMER).count() == 1
    assert (
        TelegramOutboxEvent.objects.filter(kind=TelegramOutboxEvent.Kind.CUSTOMER_MESSAGE).count()
        == 1
    )


def test_customer_ack_is_once_per_conversation_without_suppressing_messages(
    part, worker, api, operators
):
    request = _request(part, key="ack" * 10 + "12")
    link(worker, api, request)

    run(worker, api, message_update(CUSTOMER, "первое"))
    run(worker, api, message_update(CUSTOMER, "второе"))
    run(worker, api, message_update(CUSTOMER, "третье"))

    assert api.texts_to(CUSTOMER).count(CUSTOMER_ACK_TEXT) == 1
    messages = TelegramMessage.objects.filter(direction=TelegramMessage.Direction.CUSTOMER)
    assert list(messages.values_list("text", flat=True)) == ["первое", "второе", "третье"]
    assert TelegramOutboxEvent.objects.filter(
        kind=TelegramOutboxEvent.Kind.CUSTOMER_MESSAGE
    ).count() == 3
    for operator in operators:
        delivered = "\n".join(api.texts_to(operator.telegram_user_id))
        assert all(text in delivered for text in ("первое", "второе", "третье"))


def test_ack_replay_is_silent_and_next_request_gets_its_own_ack(part, worker, api, operators):
    first = _request(part, key="a" * 31 + "1")
    link(worker, api, first)
    update = message_update(CUSTOMER, "первое", update_id=444001)
    assert [item.text for item in handle_update(update)] == [CUSTOMER_ACK_TEXT]
    assert handle_update(update) == []

    second = _request(part, key="a" * 31 + "2")
    link(worker, api, second)
    assert [item.text for item in handle_update(message_update(CUSTOMER, "новая заявка"))] == [
        CUSTOMER_ACK_TEXT
    ]
    assert TelegramMessage.objects.filter(
        direction=TelegramMessage.Direction.CUSTOMER, conversation__request=first
    ).count() == 1
    assert TelegramMessage.objects.filter(
        direction=TelegramMessage.Direction.CUSTOMER, conversation__request=second
    ).count() == 1


def test_worker_restart_does_not_reset_the_durable_first_message_ack(part, worker, api, operators):
    request = _request(part, key="r" * 32)
    link(worker, api, request)
    run(worker, api, message_update(CUSTOMER, "первое"))
    worker.release()
    restarted = TelegramBotWorker(
        api, worker_id="worker-restarted", poll_timeout=0, heartbeat_file=""
    )
    restarted.start()

    run(restarted, api, message_update(CUSTOMER, "второе"))

    assert api.texts_to(CUSTOMER).count(CUSTOMER_ACK_TEXT) == 1
    assert TelegramMessage.objects.filter(direction=TelegramMessage.Direction.CUSTOMER).count() == 2


def test_two_operators_get_one_copy_and_customer_one_confirmation(part, worker, api, operators):
    request = _request(part, key="1" * 32)
    link(worker, api, request)
    for _ in range(3):
        worker.dispatch_events()
        worker.drain_outbox()
    assert len(api.texts_to(CUSTOMER)) == 1
    for operator in operators:
        texts = api.texts_to(operator.telegram_user_id)
        assert sum("НОВАЯ ЗАЯВКА" in text for text in texts) == 1
        assert sum("подключил Telegram" in text for text in texts) == 1


def test_unsupported_media_is_explained_and_not_stored(part, worker, api):
    request = _request(part, key="2" * 32)
    link(worker, api, request)
    run(worker, api, message_update(CUSTOMER, photo=[{"file_id": "abc"}]))
    assert "только текст" in api.texts_to(CUSTOMER)[-1]
    assert not TelegramMessage.objects.filter(direction=TelegramMessage.Direction.CUSTOMER).exists()


def test_group_chats_are_ignored(part, worker, api, operators):
    run(worker, api, message_update(OPERATOR_A, "/requests", chat_type="group"))
    assert not api.sent


# --- Q, R: several requests of one customer -----------------------------------------------


def test_same_customer_with_two_requests_routes_deterministically(part, worker, api, operators):
    first = _request(part, key="3" * 32)
    second = _request(part, key="4" * 32)
    link(worker, api, first)
    link(worker, api, second)
    first_conversation = TelegramConversation.objects.get(request=first)
    second_conversation = TelegramConversation.objects.get(request=second)

    run(worker, api, message_update(CUSTOMER, "про вторую"))
    assert TelegramMessage.objects.get(text="про вторую").conversation == second_conversation

    run(worker, api, callback_update(CUSTOMER, f"s:{first_conversation.public_id.hex}"))
    run(worker, api, message_update(CUSTOMER, "про первую"))
    assert TelegramMessage.objects.get(text="про первую").conversation == first_conversation

    TelegramCustomerChat.objects.update(active_conversation=None)
    run(worker, api, message_update(CUSTOMER, "непонятно про какую"))
    assert not TelegramMessage.objects.filter(text="непонятно про какую").exists()
    selector = api.last_with(CUSTOMER, "несколько заявок")["reply_markup"]["inline_keyboard"]
    assert {row[0]["callback_data"] for row in selector} == {
        f"s:{first_conversation.public_id.hex}",
        f"s:{second_conversation.public_id.hex}",
    }


def test_customer_cannot_select_or_reach_another_customers_request(part, worker, api, operators):
    mine = _request(part, key="5" * 32)
    theirs = _request(part, key="6" * 32)
    link(worker, api, mine, chat_id=CUSTOMER)
    link(worker, api, theirs, chat_id=OTHER_CUSTOMER)
    their_conversation = TelegramConversation.objects.get(request=theirs)

    run(
        worker,
        api,
        callback_update(CUSTOMER, f"s:{their_conversation.public_id.hex}"),
        callback_update(CUSTOMER, f"c:{their_conversation.public_id.hex}"),
        callback_update(CUSTOMER, f"r:{their_conversation.public_id.hex}"),
        message_update(CUSTOMER, "/requests"),
    )
    run(worker, api, message_update(CUSTOMER, "моё сообщение"))

    assert TelegramMessage.objects.get(text="моё сообщение").conversation.request == mine
    customer_text = "\n".join(api.texts_to(CUSTOMER))
    assert f"№{theirs.human_number}" not in customer_text
    assert [text for _id, text in api.answers[-3:]] == ["Недоступно."] * 3
    assert TelegramCustomerChat.objects.get(chat_id=CUSTOMER).active_conversation.request == mine


# --- T, V: history and business isolation --------------------------------------------------


def test_request_page_shows_chronological_history_with_employee(
    client, part, worker, api, operators
):
    request = _request(part, key="7" * 32)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get()
    run(worker, api, message_update(CUSTOMER, "Здравствуйте, когда можно забрать?"))
    _reply(worker, api, conversation, "Добрый день. Деталь есть, можно забрать сегодня.")
    client.force_login(operators[0].user)

    html = client.get(reverse("customer_request_detail", args=[request.pk])).content.decode()

    assert "Переписка" in html and "data-timeline" in html
    first = html.index("Здравствуйте, когда можно забрать?")
    second = html.index("Добрый день. Деталь есть, можно забрать сегодня.")
    assert html.index("Ваша заявка №") < first < second
    assert "denis" in html


def test_messaging_never_touches_stock_sales_reservations_or_repairs(part, worker, api, operators):
    before = _business_state()
    request = _request(part, key="8" * 32)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get()
    run(worker, api, message_update(CUSTOMER, "вопрос"))
    _reply(worker, api, conversation, "ответ")
    assert _business_state() == before
    request.refresh_from_db()
    assert request.status == CustomerRequest.Status.NEW


def test_withdrawn_consent_blocks_replies_and_anonymization_erases_texts(
    part, worker, api, operators
):
    request = _request(part, key="9" * 32)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get()
    run(worker, api, message_update(CUSTOMER, "мой телефон 8912"))
    withdraw_consent(request_id=request.pk)
    run(worker, api, callback_update(OPERATOR_A, f"r:{conversation.public_id.hex}"))
    assert api.last_with(OPERATOR_A, "отозвал согласие")

    anonymize_request(request_id=request.pk)
    conversation.refresh_from_db()
    assert conversation.customer_chat_id is None
    assert conversation.status == TelegramConversation.Status.CLOSED
    assert set(conversation.messages.values_list("text", flat=True)) == {""}
    # No hidden identity survives: neither the routing row nor the contact.
    assert not TelegramCustomerChat.objects.filter(chat_id=CUSTOMER).exists()
    assert not CustomerRequestMessengerContact.objects.filter(request=request).exists()
    assert not TelegramConversation.objects.filter(customer_user_id=CUSTOMER).exists()


def test_anonymizing_one_request_keeps_routing_for_the_customers_other_request(
    part, worker, api, operators
):
    first = _request(part, key="7" * 32)
    second = _request(part, key="8" * 32)
    link(worker, api, first)
    link(worker, api, second)
    withdraw_consent(request_id=first.pk)
    anonymize_request(request_id=first.pk)

    assert TelegramCustomerChat.objects.filter(chat_id=CUSTOMER).exists()
    assert TelegramConversation.objects.get(request=second).customer_chat_id == CUSTOMER
    assert not CustomerRequestMessengerContact.objects.filter(request=first).exists()


def test_no_link_can_be_issued_or_consumed_after_withdrawal_or_anonymization(part, worker, api):
    request = _request(part, key="6" * 32)
    token = issue_telegram_link(request_id=request.pk).token
    withdraw_consent(request_id=request.pk)
    with pytest.raises(MessengerLinkError):
        issue_telegram_link(request_id=request.pk)
    with pytest.raises(MessengerLinkError):
        consume_telegram_start(token=token, chat_id=CUSTOMER, user_id=CUSTOMER)
    anonymize_request(request_id=request.pk)
    with pytest.raises(MessengerLinkError):
        issue_telegram_link(request_id=request.pk)
    assert not TelegramConversation.objects.filter(customer_chat_id=CUSTOMER).exists()
    assert not CustomerRequestMessengerContact.objects.exists()


# --- W: secrets --------------------------------------------------------------------------


class _Response:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_bot_token_never_reaches_errors_logs_or_storage(part, caplog, operators):
    seen_urls = []

    def refusing_opener(request, timeout):
        seen_urls.append(request.full_url)
        raise urllib.error.HTTPError(
            request.full_url,
            500,
            f"bad {FAKE_TOKEN}",
            {},
            BytesIO(f'{{"ok":false,"error_code":500,"description":"x {FAKE_TOKEN}"}}'.encode()),
        )

    real_api = TelegramBotApi(FAKE_TOKEN, opener=refusing_opener)
    worker = TelegramBotWorker(real_api, worker_id="w", poll_timeout=0, heartbeat_file="")
    TelegramBotRuntime.objects.create(pk=1)
    worker.acquire()
    _request(part, key="0" * 32)
    formatter = RedactingFormatter("%(message)s")
    with caplog.at_level(logging.DEBUG):
        worker.dispatch_events()
        worker.send_operator_deliveries()
        with pytest.raises(TelegramApiError) as refused:
            real_api.get_updates(offset=1, timeout=0)
        logging.getLogger("apps.customer_requests.telegram_bot").error("url %s", seen_urls[0])

    assert FAKE_TOKEN in seen_urls[0]  # it is used, only in the request URL
    assert FAKE_TOKEN not in str(refused.value)
    assert FAKE_TOKEN not in repr(real_api)
    assert all(FAKE_TOKEN not in formatter.format(record) for record in caplog.records)
    stored = "".join(TelegramDelivery.objects.values_list("last_error", flat=True))
    assert stored and FAKE_TOKEN not in stored
    assert FAKE_TOKEN not in redact(f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe")


def test_command_refuses_to_run_without_token_and_prints_no_secret(db, settings):
    settings.TELEGRAM_BOT_TOKEN = ""
    with pytest.raises(CommandError, match="не настроен"):
        call_command("run_telegram_bot", "--once")


def test_public_runtime_never_holds_the_bot_token():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "config/settings/public.py"
    ).read_text(encoding="utf-8")
    assert 'TELEGRAM_BOT_TOKEN = ""' in source


# --- X: restart, retries, single consumer -------------------------------------------------


def test_restart_marks_interrupted_sends_uncertain_and_resumes_pending(part, api, operators):
    request = _request(part, key="i" * 32)
    first = TelegramBotWorker(api, worker_id="first", poll_timeout=0, heartbeat_file="")
    first.start()
    link(first, api, request)
    conversation = TelegramConversation.objects.get()
    interrupted = TelegramMessage.objects.create(
        conversation=conversation,
        direction=TelegramMessage.Direction.OPERATOR,
        text="прерванное",
        delivery_status=TelegramDeliveryStatus.SENDING,
        attempts=1,
    )
    waiting = TelegramMessage.objects.create(
        conversation=conversation,
        direction=TelegramMessage.Direction.OPERATOR,
        text="ждущее",
        delivery_status=TelegramDeliveryStatus.PENDING,
        next_attempt_at=timezone.now(),
    )
    # The first process dies without releasing its lease; it expires.
    TelegramBotRuntime.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))

    second = TelegramBotWorker(api, worker_id="second", poll_timeout=0, heartbeat_file="")
    second.start()
    second.iterate(poll_timeout=0)

    interrupted.refresh_from_db()
    waiting.refresh_from_db()
    assert interrupted.delivery_status == TelegramDeliveryStatus.UNCERTAIN
    assert "прерванное" not in api.texts_to(CUSTOMER)
    assert waiting.delivery_status == TelegramDeliveryStatus.SENT
    assert api.texts_to(CUSTOMER).count("ждущее") == 1


def test_second_worker_is_refused_while_the_lease_is_held(db, api):
    TelegramBotWorker(api, worker_id="one", heartbeat_file="").acquire()
    with pytest.raises(SingleInstanceError):
        TelegramBotWorker(api, worker_id="two", heartbeat_file="").acquire()


def test_webhook_configured_or_conflict_stops_the_worker(db, api):
    api.webhook = {"url": "https://example.invalid/hook"}
    with pytest.raises(SingleInstanceError, match="webhook"):
        TelegramBotWorker(api, worker_id="a", heartbeat_file="").run(once=True)
    # A refused start releases the lease at once: the next start is not blocked.
    assert TelegramBotRuntime.objects.get().worker_id == ""

    api.webhook = {"url": ""}
    api.get_updates_error = TelegramApiError(409, "Conflict: terminated by other getUpdates")
    bot = TelegramBotWorker(api, worker_id="b", poll_timeout=0, heartbeat_file="")
    with pytest.raises(SingleInstanceError, match="409"):
        bot.run(once=True)
    assert TelegramBotRuntime.objects.get().worker_id == ""


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TelegramApiError(500, "Internal"), TelegramDeliveryStatus.PENDING),
        (
            TelegramApiError(429, "Too Many Requests", retry_after=33),
            TelegramDeliveryStatus.PENDING,
        ),
        (
            TelegramApiError(403, "Forbidden: bot was blocked by the user"),
            TelegramDeliveryStatus.FAILED,
        ),
        (TelegramNetworkError("timeout", ambiguous=True), TelegramDeliveryStatus.UNCERTAIN),
        (
            TelegramNetworkError("ConnectionRefusedError", ambiguous=False),
            TelegramDeliveryStatus.PENDING,
        ),
    ],
)
def test_customer_delivery_failure_classes(part, worker, api, operators, failure, expected):
    request = _request(part, key="l" * 32)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get()
    TelegramMessage.objects.create(
        conversation=conversation,
        direction=TelegramMessage.Direction.OPERATOR,
        text="ответ",
        delivery_status=TelegramDeliveryStatus.PENDING,
        next_attempt_at=timezone.now(),
    )
    api.failures = [failure]
    worker.send_customer_messages()
    row = TelegramMessage.objects.get(text="ответ")
    assert row.delivery_status == expected
    assert row.last_error
    if expected == TelegramDeliveryStatus.PENDING:
        delay = (row.next_attempt_at - timezone.now()).total_seconds()
        assert delay > (30 if getattr(failure, "retry_after", None) else 5)


def test_retries_are_bounded(part, worker, api, operators):
    request = _request(part, key="o" * 32)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get()
    row = TelegramMessage.objects.create(
        conversation=conversation,
        direction=TelegramMessage.Direction.OPERATOR,
        text="ответ",
        delivery_status=TelegramDeliveryStatus.PENDING,
        next_attempt_at=timezone.now(),
        attempts=MAX_ATTEMPTS - 1,
    )
    api.failures = [TelegramApiError(502, "Bad Gateway")]
    worker.send_customer_messages()
    row.refresh_from_db()
    assert row.delivery_status == TelegramDeliveryStatus.FAILED


# --- Internal operator management ----------------------------------------------------------


def test_operator_management_is_admin_only_and_validated(client, django_user_model, part):
    seller = django_user_model.objects.create_user(username="seller", password="x" * 12)
    seller.groups.add(Group.objects.get(name=roles.SELLER))
    viewer = django_user_model.objects.create_user(username="viewer", password="x" * 12)
    viewer.groups.add(Group.objects.get(name=roles.VIEWER))
    admin = django_user_model.objects.create_superuser(username="owner", password="x" * 12)
    url = reverse("telegram_settings")

    client.force_login(seller)
    assert client.get(url).status_code == 403
    assert (
        client.post(url, {"user": seller.pk, "telegram_user_id": 5, "role": "operator"}).status_code
        == 403
    )

    client.force_login(admin)
    page = client.get(url)
    assert page.status_code == 200
    assert "Не запущен или не настроен" in page.content.decode()
    assert (
        client.post(url, {"user": viewer.pk, "telegram_user_id": 5, "role": "operator"}).status_code
        == 200
    )
    assert not TelegramOperator.objects.exists()  # no access to requests: refused
    client.post(url, {"user": seller.pk, "telegram_user_id": OPERATOR_A, "role": "operator"})
    operator = TelegramOperator.objects.get()
    assert operator.created_by == admin
    other = django_user_model.objects.create_user(username="seller2", password="x" * 12)
    other.groups.add(Group.objects.get(name=roles.SELLER))
    collision = client.post(
        url, {"user": other.pk, "telegram_user_id": OPERATOR_A, "role": "operator"}
    )
    assert "уже привязан" in collision.content.decode()
    assert (
        client.post(url, {"user": other.pk, "telegram_user_id": -4, "role": "operator"}).status_code
        == 200
    )
    assert TelegramOperator.objects.count() == 1

    client.post(reverse("telegram_operator_toggle", args=[operator.pk]))
    operator.refresh_from_db()
    assert operator.is_active is False
    client.post(reverse("telegram_operator_role", args=[operator.pk]), {"role": "admin"})
    operator.refresh_from_db()
    assert operator.role == "admin"
    assert FAKE_TOKEN not in client.get(url).content.decode()


@override_settings(TELEGRAM_BOT_TOKEN=FAKE_TOKEN)
def test_status_page_never_renders_the_token(client, django_user_model, db):
    admin = django_user_model.objects.create_superuser(username="owner", password="x" * 12)
    TelegramBotRuntime.objects.create(
        pk=1, heartbeat_at=timezone.now(), worker_id="w", bot_username="ProStorTestBot"
    )
    client.force_login(admin)
    html = client.get(reverse("telegram_settings")).content.decode()
    assert "Работает" in html and "@ProStorTestBot" in html
    assert FAKE_TOKEN not in html and "AAFake" not in html


# --- Independent review regressions ---------------------------------------------------------


def test_refused_second_instance_never_touches_the_running_workers_lease(db, api, monkeypatch):
    first = TelegramBotWorker(api, worker_id="first", poll_timeout=0, heartbeat_file="")
    first.acquire()
    # The first worker is alive but stalled past its lease; its lock is still held.
    TelegramBotRuntime.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    second = TelegramBotWorker(api, worker_id="second", poll_timeout=0, heartbeat_file="")

    def lock_held():
        raise SingleInstanceError("Другой экземпляр Telegram-бота держит блокировку.")

    monkeypatch.setattr(second, "_lock_database", lock_held)
    with pytest.raises(SingleInstanceError):
        second.run(once=True)

    assert TelegramBotRuntime.objects.get().worker_id == "first"
    first.renew()  # the running worker keeps its lease and continues


def test_database_error_mid_send_never_leaves_a_message_invisible_or_resent(
    part, worker, api, operators, monkeypatch
):
    request = _request(part, key="5" * 32)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get()
    row = TelegramMessage.objects.create(
        conversation=conversation,
        direction=TelegramMessage.Direction.OPERATOR,
        text="ответ при сбое базы",
        delivery_status=TelegramDeliveryStatus.PENDING,
        next_attempt_at=timezone.now(),
    )
    real_finish = worker._finish

    def database_down(*args, **kwargs):
        raise DatabaseError("connection lost")

    monkeypatch.setattr(worker, "_finish", database_down)
    monkeypatch.setattr(worker.stop, "wait", lambda seconds: None)
    monkeypatch.setattr(connection, "close", lambda: None)
    worker.run(once=True)
    row.refresh_from_db()
    assert row.delivery_status == TelegramDeliveryStatus.SENDING
    sent_during_failure = api.texts_to(CUSTOMER).count("ответ при сбое базы")

    monkeypatch.setattr(worker, "_finish", real_finish)
    worker.acquire()
    worker.iterate(poll_timeout=0)
    worker.iterate(poll_timeout=0)
    row.refresh_from_db()
    assert row.delivery_status == TelegramDeliveryStatus.UNCERTAIN
    assert row.last_error
    assert api.texts_to(CUSTOMER).count("ответ при сбое базы") == sent_during_failure == 1


@pytest.mark.parametrize(
    ("body", "method", "ambiguous"),
    [
        (b'{"ok":false,"error_code":"boom","description":"x"}', "get_me", False),
        (b'{"ok":false,"error_code":true}', "get_me", False),
        (b'{"ok":"yes","result":{}}', "get_me", False),
        (b'{"ok":true,"result":[1]}', "send", True),
        (b'{"ok":true,"result":{"update_id":1}}', "updates", False),
        (b"[]", "get_me", False),
    ],
)
def test_malformed_telegram_answers_are_network_errors_not_crashes(body, method, ambiguous):
    api = TelegramBotApi(FAKE_TOKEN, opener=lambda request, timeout: _Response(body))
    with pytest.raises(TelegramNetworkError) as error:
        if method == "get_me":
            api.get_me()
        elif method == "send":
            api.send_message(chat_id=CUSTOMER, text="x")
        else:
            api.get_updates(offset=1, timeout=0)
    assert error.value.ambiguous is ambiguous
    assert FAKE_TOKEN not in str(error.value)


def test_retry_after_from_telegram_is_bounded(db):
    body = (
        b'{"ok":false,"error_code":429,"description":"Too Many",'
        b'"parameters":{"retry_after":999999}}'
    )
    api = TelegramBotApi(FAKE_TOKEN, opener=lambda request, timeout: _Response(body))
    with pytest.raises(TelegramApiError) as error:
        api.get_me()
    assert error.value.error_code == 429 and error.value.retry_after is None


@pytest.mark.django_db(transaction=True)
def test_postgresql_second_worker_never_takes_the_lock_or_lease_of_a_live_worker(api):
    if connection.vendor != "postgresql":
        pytest.skip("The advisory lock needs PostgreSQL")
    live = TelegramBotWorker(api, worker_id="live", poll_timeout=0, heartbeat_file="")
    live.acquire()
    # Stalled past its lease, but its database session and lock are alive.
    TelegramBotRuntime.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=5))
    outcome = {}

    def second_container():
        try:
            TelegramBotWorker(
                FakeBotApi(), worker_id="second", poll_timeout=0, heartbeat_file=""
            ).run(once=True)
            outcome["result"] = "ran"
        except SingleInstanceError as exc:
            outcome["result"] = str(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=second_container)
    thread.start()
    thread.join(30)
    assert "блокировку" in outcome["result"]
    assert TelegramBotRuntime.objects.get().worker_id == "live"
    live.renew()

    # SIGKILL of the live worker: its session ends and PostgreSQL drops the lock.
    connection.close()
    TelegramBotRuntime.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    successor = TelegramBotWorker(api, worker_id="successor", poll_timeout=0, heartbeat_file="")
    successor.acquire()
    assert TelegramBotRuntime.objects.get().worker_id == "successor"
    successor.release()


# --- Zero-recipient operator_reply events ------------------------------------------------


@pytest.fixture
def solo_operator(db, django_user_model):
    """The real production shape: exactly one person answers customers."""
    return _operator(django_user_model, OPERATOR_A, username="solo")


def _replied_event(worker, api, part, *, key):
    request = _request(part, key=key)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get(request=request)
    run(worker, api, message_update(CUSTOMER, "первое тестовое сообщение"))
    _reply(worker, api, conversation, "тестовый ответ менеджера")
    worker.drain_outbox()
    return request, conversation


def test_reply_by_the_only_operator_needs_no_notification_and_completes(
    part, worker, api, solo_operator
):
    _replied_event(worker, api, part, key="z1" * 16)

    event = TelegramOutboxEvent.objects.get(kind=TelegramOutboxEvent.Kind.OPERATOR_REPLY)
    assert event.exclude_operator_id == solo_operator.pk
    assert event.status == TelegramOutboxEvent.Status.DISPATCHED
    assert event.dispatched_at is not None
    assert not TelegramDelivery.objects.filter(event=event).exists()
    assert event.attempts == 0


def test_zero_recipient_reply_event_stops_being_due_work(part, worker, api, solo_operator):
    _replied_event(worker, api, part, key="z2" * 16)

    assert not TelegramOutboxEvent.objects.filter(
        status=TelegramOutboxEvent.Status.PENDING
    ).exists()
    assert not worker.has_due_work()


def test_redispatching_a_zero_recipient_reply_event_is_idempotent(part, worker, api, solo_operator):
    _replied_event(worker, api, part, key="z3" * 16)
    event = TelegramOutboxEvent.objects.get(kind=TelegramOutboxEvent.Kind.OPERATOR_REPLY)
    dispatched_at, attempts = event.dispatched_at, event.attempts

    worker.dispatch_events()
    worker.dispatch_events()

    event.refresh_from_db()
    assert event.status == TelegramOutboxEvent.Status.DISPATCHED
    assert event.dispatched_at == dispatched_at
    assert event.attempts == attempts
    assert not TelegramDelivery.objects.filter(event=event).exists()


def test_worker_restart_does_not_reopen_a_completed_zero_recipient_event(
    part, worker, api, solo_operator
):
    _replied_event(worker, api, part, key="z4" * 16)
    worker.release()
    successor = TelegramBotWorker(api, worker_id="successor", poll_timeout=0, heartbeat_file="")
    successor.start()

    successor.drain_outbox()

    event = TelegramOutboxEvent.objects.get(kind=TelegramOutboxEvent.Kind.OPERATOR_REPLY)
    assert event.status == TelegramOutboxEvent.Status.DISPATCHED
    assert not TelegramDelivery.objects.filter(event=event).exists()
    successor.release()


def test_an_operator_hired_later_never_hears_an_old_self_notification(
    part, worker, api, solo_operator, django_user_model
):
    _replied_event(worker, api, part, key="z5" * 16)
    event = TelegramOutboxEvent.objects.get(kind=TelegramOutboxEvent.Kind.OPERATOR_REPLY)

    latecomer = _operator(django_user_model, OPERATOR_B, username="latecomer")
    worker.drain_outbox()

    assert not TelegramDelivery.objects.filter(event=event).exists()
    assert not api.texts_to(latecomer.telegram_user_id)


def test_the_customer_still_receives_the_reply_of_the_only_operator(
    part, worker, api, solo_operator
):
    _replied_event(worker, api, part, key="z6" * 16)

    assert api.texts_to(CUSTOMER).count("тестовый ответ менеджера") == 1
    reply = TelegramMessage.objects.get(direction=TelegramMessage.Direction.OPERATOR)
    assert reply.operator_user == solo_operator.user
    assert reply.delivery_status == TelegramDeliveryStatus.SENT
    assert "solo" not in "\n".join(api.texts_to(CUSTOMER))


def test_a_second_operator_still_hears_about_a_colleagues_reply(part, worker, api, operators):
    request = _request(part, key="z7" * 16)
    link(worker, api, request)
    conversation = TelegramConversation.objects.get(request=request)

    _reply(worker, api, conversation, "ответ первого сотрудника")
    worker.drain_outbox()

    event = TelegramOutboxEvent.objects.get(kind=TelegramOutboxEvent.Kind.OPERATOR_REPLY)
    assert event.status == TelegramOutboxEvent.Status.DISPATCHED
    deliveries = TelegramDelivery.objects.filter(event=event)
    assert [row.operator_id for row in deliveries] == [operators[1].pk]
    assert any("ответ первого сотрудника" in text for text in api.texts_to(OPERATOR_B))


def test_customer_message_fan_out_is_unchanged_for_a_single_operator(
    part, worker, api, solo_operator
):
    request = _request(part, key="z8" * 16)
    link(worker, api, request)

    run(worker, api, message_update(CUSTOMER, "первое тестовое сообщение"))
    run(worker, api, message_update(CUSTOMER, "второе тестовое сообщение"))
    worker.drain_outbox()

    events = TelegramOutboxEvent.objects.filter(kind=TelegramOutboxEvent.Kind.CUSTOMER_MESSAGE)
    assert events.count() == 2
    assert {event.status for event in events} == {TelegramOutboxEvent.Status.DISPATCHED}
    for event in events:
        assert [row.operator_id for row in event.deliveries.all()] == [solo_operator.pk]
    delivered = "\n".join(api.texts_to(OPERATOR_A))
    assert "первое тестовое сообщение" in delivered
    assert "второе тестовое сообщение" in delivered
    assert api.texts_to(CUSTOMER).count(CUSTOMER_ACK_TEXT) == 1


def test_new_request_without_any_operator_still_waits_instead_of_completing(part, worker, api):
    _request(part, key="z9" * 16)

    worker.dispatch_events()

    event = TelegramOutboxEvent.objects.get(kind=TelegramOutboxEvent.Kind.NEW_REQUEST)
    assert event.exclude_operator_id is None
    assert event.status == TelegramOutboxEvent.Status.PENDING
    assert event.dispatched_at is None
    assert event.attempts == 1


def test_cancelling_the_request_keeps_the_completed_reply_history(part, worker, api, solo_operator):
    request, conversation = _replied_event(worker, api, part, key="za" * 16)

    change_request_status(
        request_id=request.pk,
        target_status=CustomerRequest.Status.CANCELED,
        by=solo_operator.user,
    )

    request.refresh_from_db()
    assert request.status == CustomerRequest.Status.CANCELED
    history = list(conversation.messages.values_list("direction", flat=True))
    assert history == [
        TelegramMessage.Direction.SYSTEM,
        TelegramMessage.Direction.CUSTOMER,
        TelegramMessage.Direction.OPERATOR,
    ]
    assert TelegramOutboxEvent.objects.get(
        kind=TelegramOutboxEvent.Kind.OPERATOR_REPLY
    ).status == TelegramOutboxEvent.Status.DISPATCHED


def test_a_replayed_first_customer_message_cannot_produce_a_second_ack(
    part, worker, api, solo_operator
):
    request = _request(part, key="zb" * 16)
    link(worker, api, request)
    update = message_update(CUSTOMER, "первое тестовое сообщение", update_id=555001)

    # The worker delivers the acknowledgement the customer actually sees.
    run(worker, api, update)
    # Every later delivery of the same update is silent, however it arrives.
    assert handle_update(update) == []
    assert handle_update(update) == []
    run(worker, api, update)

    assert api.texts_to(CUSTOMER).count(CUSTOMER_ACK_TEXT) == 1
    assert TelegramMessage.objects.filter(direction=TelegramMessage.Direction.CUSTOMER).count() == 1
    assert (
        TelegramOutboxEvent.objects.filter(kind=TelegramOutboxEvent.Kind.CUSTOMER_MESSAGE).count()
        == 1
    )


def test_serialized_first_messages_ack_once_and_all_of_them_reach_the_operator(
    part, worker, api, solo_operator
):
    request = _request(part, key="zc" * 16)
    link(worker, api, request)

    acks = []
    for text in ("первое", "второе", "третье"):
        acks.extend(
            item.text for item in handle_update(message_update(CUSTOMER, text)) if item.text
        )
    worker.drain_outbox()

    assert acks == [CUSTOMER_ACK_TEXT]
    assert list(
        TelegramMessage.objects.filter(direction=TelegramMessage.Direction.CUSTOMER).values_list(
            "text", flat=True
        )
    ) == ["первое", "второе", "третье"]
    delivered = "\n".join(api.texts_to(OPERATOR_A))
    assert all(text in delivered for text in ("первое", "второе", "третье"))


def test_replaying_start_does_not_duplicate_the_order_summary(part, worker, api, solo_operator):
    request = _request(part, key="zd" * 16)
    token = link(worker, api, request)

    run(worker, api, message_update(CUSTOMER, f"/start {token}"))
    worker.drain_outbox()

    summaries = [text for text in api.texts_to(CUSTOMER) if "Ваш заказ" in text]
    assert len(summaries) == 1
    assert (
        TelegramMessage.objects.filter(
            conversation__request=request, direction=TelegramMessage.Direction.SYSTEM
        ).count()
        == 1
    )


# --- Stage 0: a closed request never takes customer messages -------------------------------


def _telegram_customer_message_events():
    return TelegramOutboxEvent.objects.filter(
        kind=TelegramOutboxEvent.Kind.CUSTOMER_MESSAGE
    ).count()


def test_telegram_cancelled_current_request_refuses_the_message_and_offers_the_open_ones(
    part, worker, api, operators
):
    first = _request(part, key="t0" * 16)
    second = _request(part, key="t1" * 16)
    link(worker, api, first)
    link(worker, api, second)
    first_conversation = TelegramConversation.objects.get(request=first)
    second_conversation = TelegramConversation.objects.get(request=second)
    run(worker, api, message_update(CUSTOMER, "про вторую"))
    events = _telegram_customer_message_events()

    change_request_status(
        request_id=second.pk,
        target_status=CustomerRequest.Status.CANCELED,
        by=operators[0].user,
    )
    run(worker, api, message_update(CUSTOMER, "ещё про вторую"))

    assert not TelegramMessage.objects.filter(text="ещё про вторую").exists()
    assert _telegram_customer_message_events() == events
    assert "ещё про вторую" not in "\n".join(api.texts_to(OPERATOR_A))
    reply = api.last_with(CUSTOMER, "уже закрыта")
    assert reply["text"] == messaging.closed_request_text(second.reference, other_open=True)
    assert {row[0]["callback_data"] for row in reply["reply_markup"]["inline_keyboard"]} == {
        f"s:{first_conversation.public_id.hex}"
    }
    # Never silently moved to the other request; the choice and history stay.
    assert TelegramCustomerChat.objects.get(chat_id=CUSTOMER).active_conversation == (
        second_conversation
    )
    assert list(
        TelegramMessage.objects.filter(
            conversation=second_conversation, direction=TelegramMessage.Direction.CUSTOMER
        ).values_list("text", flat=True)
    ) == ["про вторую"]

    run(worker, api, callback_update(CUSTOMER, f"s:{first_conversation.public_id.hex}"))
    run(worker, api, message_update(CUSTOMER, "теперь про первую"))
    assert TelegramMessage.objects.get(text="теперь про первую").conversation == first_conversation


def test_telegram_completed_only_request_refuses_messages_and_offers_nothing(
    part, worker, api, operators
):
    request = _request(part, key="t2" * 16)
    link(worker, api, request)
    run(worker, api, message_update(CUSTOMER, "спасибо"))
    change_request_status(
        request_id=request.pk,
        target_status=CustomerRequest.Status.IN_PROGRESS,
        by=operators[0].user,
    )
    change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.COMPLETED, by=operators[0].user
    )
    events = _telegram_customer_message_events()

    run(worker, api, message_update(CUSTOMER, "а ещё вопрос"))

    assert not TelegramMessage.objects.filter(text="а ещё вопрос").exists()
    assert _telegram_customer_message_events() == events
    reply = api.last_with(CUSTOMER, "уже закрыта")
    assert reply["text"] == messaging.closed_request_text(request.reference, other_open=False)
    # Nothing to switch to: no request buttons, only the customer's own keyboard.
    assert not (reply["reply_markup"] or {}).get("inline_keyboard")
    assert reply["reply_markup"] == service_module.customer_keyboard()


def test_telegram_requests_hide_closed_requests_and_a_stale_button_changes_nothing(
    part, worker, api, operators
):
    first = _request(part, key="t3" * 16)
    second = _request(part, key="t4" * 16)
    link(worker, api, first)
    link(worker, api, second)
    first_conversation = TelegramConversation.objects.get(request=first)
    second_conversation = TelegramConversation.objects.get(request=second)
    change_request_status(
        request_id=first.pk, target_status=CustomerRequest.Status.CANCELED, by=operators[0].user
    )

    run(worker, api, message_update(CUSTOMER, "Мои заявки"))  # the keyboard button
    listing = api.last_with(CUSTOMER, "активная заявка")["reply_markup"]["inline_keyboard"]
    assert {row[0]["callback_data"] for row in listing} == {
        f"s:{second_conversation.public_id.hex}"
    }

    stale = callback_update(CUSTOMER, f"s:{first_conversation.public_id.hex}")
    run(worker, api, stale)
    run(worker, api, stale)  # the same callback delivered again

    closed_text = messaging.closed_request_text(first.reference, other_open=True)
    assert api.texts_to(CUSTOMER).count(closed_text) == 1
    assert TelegramCustomerChat.objects.get(chat_id=CUSTOMER).active_conversation == (
        second_conversation
    )
    run(worker, api, message_update(CUSTOMER, "про вторую"))
    assert TelegramMessage.objects.get(text="про вторую").conversation == second_conversation

    # Another customer's press on this closed request learns nothing about it.
    theirs = _request(part, key="t5" * 16)
    link(worker, api, theirs, chat_id=OTHER_CUSTOMER)
    run(worker, api, callback_update(OTHER_CUSTOMER, f"s:{first_conversation.public_id.hex}"))
    assert api.answers[-1][1] == "Недоступно."
    assert f"№{first.human_number}" not in "\n".join(api.texts_to(OTHER_CUSTOMER))


def test_telegram_completed_request_can_neither_issue_nor_consume_a_link(
    part, worker, api, operators
):
    request = _request(part, key="t6" * 16)
    token = issue_telegram_link(request_id=request.pk).token
    change_request_status(
        request_id=request.pk,
        target_status=CustomerRequest.Status.IN_PROGRESS,
        by=operators[0].user,
    )
    change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.COMPLETED, by=operators[0].user
    )

    with pytest.raises(MessengerLinkError):
        issue_telegram_link(request_id=request.pk)
    run(worker, api, message_update(CUSTOMER, f"/start {token}"))

    assert not TelegramConversation.objects.get(request=request).is_linked


def test_the_customer_keyboard_opens_my_requests_and_switching_is_one_line(
    part, worker, api, operators
):
    """Release B: buttons, not commands, and a short confirmation on a switch."""
    first = _request(part, key="kb1" * 10 + "12")
    second = _request(part, key="kb2" * 10 + "12")
    link(worker, api, first)
    link(worker, api, second)

    # The greeting already carries the customer's own keyboard.
    run(worker, api, message_update(CUSTOMER, "/start"))
    hello = api.last_with(CUSTOMER, "Напишите сообщение")
    assert hello["reply_markup"]["keyboard"] == [[{"text": "Мои заявки"}]]
    assert "Все заявки" not in str(hello["reply_markup"])
    assert "Загрузка фото по продажам/ремонтам" not in str(hello["reply_markup"])

    # Pressing it sends plain text, and the bot answers with the selector.
    run(worker, api, message_update(CUSTOMER, "Мои заявки"))
    selector = api.last_with(CUSTOMER, "Мои активные заявки:")
    labels = [row[0]["text"] for row in selector["reply_markup"]["inline_keyboard"]]
    assert labels == [f"✓ №{second.reference}", f"№{first.reference}"]
    assert not TelegramMessage.objects.filter(text="Мои заявки").exists()

    # Choosing the other request confirms in one line, without a second greeting.
    first_conversation = TelegramConversation.objects.get(request=first)
    run(worker, api, callback_update(CUSTOMER, f"s:{first_conversation.public_id.hex}"))
    assert api.texts_to(CUSTOMER)[-1] == f"Выбрана заявка №{first.reference}."
    assert sum(text.startswith("Добрый день!") for text in api.texts_to(CUSTOMER)) == 2

    run(worker, api, message_update(CUSTOMER, "вопрос по первой"))
    assert TelegramMessage.objects.get(text="вопрос по первой").conversation == first_conversation
