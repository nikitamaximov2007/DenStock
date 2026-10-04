"""Staff alerts stay in the customer's messenger; request cards show whole quantities.

Channel affinity: a Telegram customer's request or message alerts Telegram
staff bindings only, a MAX customer's only MAX ones. Several staff bindings
of the right messenger are all alerted, once each. Staff may still open and
answer any request from either messenger: only the alerts are routed.

Every scenario asserts the notification queue in the database and what each
real worker (with a fake messenger API) actually sends.
"""
from decimal import Decimal

import pytest
from django.test import override_settings

from apps.customer_requests import max_bot, operator_console
from apps.customer_requests.max_bot import MaxBotWorker, SendPacer
from apps.customer_requests.messengers import (
    consume_max_start,
    consume_telegram_start,
    issue_max_link,
    issue_telegram_link,
)
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestLine,
    MaxMessage,
    MaxOperatorDelivery,
    MaxOutboxEvent,
    OperatorNotification,
    StaffMessengerBinding,
    TelegramMessage,
)
from apps.customer_requests.telegram_bot import TelegramBotWorker, handle_update

from .max_fake import message_created
from .test_telegram_customer_messaging import (
    FakeBotApi,
    _operator,
    _request,
    build_part,
    message_update,
)

pytestmark = pytest.mark.django_db
console_on = override_settings(
    CUSTOMER_OPERATOR_CONSOLE_ENABLED=True, CUSTOMER_MESSENGER_CABINET_ENABLED=True
)

TG_CUSTOMER = 931_001
MAX_CUSTOMER_USER = 941_001
MAX_CUSTOMER_CHAT = 951_001


class FakeMaxApi:
    """Records what the MAX worker sends; never touches the network."""

    def __init__(self):
        self.calls = []

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return {"body": {"mid": f"mid-{len(self.calls)}"}}


@pytest.fixture
def staff(django_user_model):
    """Two employees, each in both messengers, plus a Telegram-only employee."""
    denis = _operator(django_user_model, 960_001, username="route-denis").user
    rim = _operator(django_user_model, 960_002, username="route-rim").user
    olga = _operator(django_user_model, 960_003, username="route-olga").user
    make = StaffMessengerBinding.objects.create
    return {
        "tg": [
            make(user=denis, operator_key="DENIS", provider="telegram",
                 provider_user_id=970_001, customer_visible_label="Денис"),
            make(user=rim, operator_key="RIM", provider="telegram",
                 provider_user_id=970_002, customer_visible_label="Рим"),
            make(user=olga, operator_key="OLGA", provider="telegram",
                 provider_user_id=970_003, customer_visible_label="Ольга"),
        ],
        "max": [
            make(user=denis, operator_key="DENIS", provider="max", provider_user_id=980_001,
                 delivery_chat_id=990_001, customer_visible_label="Денис"),
            make(user=rim, operator_key="RIM", provider="max", provider_user_id=980_002,
                 delivery_chat_id=990_002, customer_visible_label="Рим"),
        ],
    }


def _alerts(request, kind):
    return OperatorNotification.objects.filter(request=request, kind=kind)


def _providers(request, kind):
    return sorted(_alerts(request, kind).values_list("binding__provider", flat=True))


def _deliver_both():
    """Run each real worker's console sender against a fake messenger API."""
    tg_api = FakeBotApi()
    tg_worker = TelegramBotWorker(tg_api, worker_id="route-tg", poll_timeout=0,
                                  heartbeat_file="")
    tg_worker.send_operator_console_notifications()
    max_api = FakeMaxApi()
    max_worker = MaxBotWorker(max_api, worker_id="route-max", heartbeat_file="",
                              pacer=SendPacer(sleep=lambda s: None))
    max_worker.send_operator_console_notifications()
    return tg_api, max_api


# --- A / B: new requests --------------------------------------------------------------


@console_on
def test_a_telegram_request_alerts_telegram_staff_only(staff):
    request = _request(build_part(), key="T" * 32,
                       messenger=CustomerRequest.Messenger.TELEGRAM)

    operator_console.queue_operator_notifications(since=request.created_at)

    new = OperatorNotification.Kind.NEW_REQUEST
    assert _providers(request, new) == ["telegram"] * 3
    assert {n.binding_id for n in _alerts(request, new)} == {b.pk for b in staff["tg"]}
    tg_api, max_api = _deliver_both()
    assert sorted(item["chat_id"] for item in tg_api.sent) == [970_001, 970_002, 970_003]
    assert all(f"№{request.reference}" in item["text"] for item in tg_api.sent)
    assert max_api.calls == []


@console_on
def test_b_max_request_alerts_max_staff_only(staff):
    request = _request(build_part(), key="M" * 32, messenger=CustomerRequest.Messenger.MAX)

    operator_console.queue_operator_notifications(since=request.created_at)

    new = OperatorNotification.Kind.NEW_REQUEST
    assert _providers(request, new) == ["max", "max"]
    tg_api, max_api = _deliver_both()
    assert tg_api.sent == []
    assert sorted(call["chat_id"] for call in max_api.calls) == [990_001, 990_002]


# --- C / D: customer messages ---------------------------------------------------------


@console_on
def test_c_telegram_customer_message_alerts_telegram_staff_only(staff):
    request = _request(build_part(), key="C" * 32,
                       messenger=CustomerRequest.Messenger.TELEGRAM)
    token = issue_telegram_link(request_id=request.pk).token
    consume_telegram_start(token=token, chat_id=TG_CUSTOMER, user_id=TG_CUSTOMER)
    update = message_update(TG_CUSTOMER, "Есть в наличии?")
    handle_update(update)
    assert TelegramMessage.objects.filter(direction=TelegramMessage.Direction.CUSTOMER).exists()

    operator_console.queue_operator_notifications(since=request.created_at)

    message = OperatorNotification.Kind.CUSTOMER_MESSAGE
    assert _providers(request, message) == ["telegram"] * 3
    tg_api, max_api = _deliver_both()
    assert any("Есть в наличии?" in item["text"] for item in tg_api.sent)
    assert max_api.calls == []


@console_on
def test_d_max_customer_message_alerts_max_staff_only(staff):
    request = _request(build_part(), key="D" * 32, messenger=CustomerRequest.Messenger.MAX)
    token = issue_max_link(request_id=request.pk).token
    consume_max_start(token=token, chat_id=MAX_CUSTOMER_CHAT, user_id=MAX_CUSTOMER_USER)
    max_bot.handle_update(message_created(MAX_CUSTOMER_USER, MAX_CUSTOMER_CHAT, "Когда будет?"))
    assert MaxMessage.objects.filter(direction=MaxMessage.Direction.CUSTOMER).exists()

    operator_console.queue_operator_notifications(since=request.created_at)

    message = OperatorNotification.Kind.CUSTOMER_MESSAGE
    assert _providers(request, message) == ["max", "max"]
    tg_api, max_api = _deliver_both()
    assert tg_api.sent == []
    assert any("Когда будет?" in call["text"] for call in max_api.calls)


# --- E: deduplication -------------------------------------------------------------------


@console_on
def test_e_redelivery_and_repeated_queueing_alert_each_staff_binding_once(staff):
    tg_request = _request(build_part(), key="E" * 32,
                          messenger=CustomerRequest.Messenger.TELEGRAM)
    token = issue_telegram_link(request_id=tg_request.pk).token
    consume_telegram_start(token=token, chat_id=TG_CUSTOMER, user_id=TG_CUSTOMER)
    tg_update = message_update(TG_CUSTOMER, "повтор")
    handle_update(tg_update)
    handle_update(tg_update)  # the same update again

    since = tg_request.created_at
    for _ in range(3):  # both workers queue on every cycle
        operator_console.queue_operator_notifications(since=since)

    assert _alerts(tg_request, OperatorNotification.Kind.NEW_REQUEST).count() == 3
    assert _alerts(tg_request, OperatorNotification.Kind.CUSTOMER_MESSAGE).count() == 3
    tg_api, max_api = _deliver_both()
    assert len(tg_api.sent) == 6
    assert max_api.calls == []
    tg_api_again, max_api_again = _deliver_both()
    assert tg_api_again.sent == [] and max_api_again.calls == []


@console_on
def test_e_max_webhook_redelivery_alerts_each_max_binding_once(staff):
    request = _request(build_part(), key="G" * 32, messenger=CustomerRequest.Messenger.MAX)
    token = issue_max_link(request_id=request.pk).token
    consume_max_start(token=token, chat_id=MAX_CUSTOMER_CHAT, user_id=MAX_CUSTOMER_USER)
    update = message_created(MAX_CUSTOMER_USER, MAX_CUSTOMER_CHAT, "дубль", mid="mid.route.1")
    max_bot.handle_update(update)
    max_bot.handle_update(update)  # MAX redelivers the same mid

    for _ in range(3):
        operator_console.queue_operator_notifications(since=request.created_at)

    assert MaxMessage.objects.filter(direction=MaxMessage.Direction.CUSTOMER).count() == 1
    assert _alerts(request, OperatorNotification.Kind.CUSTOMER_MESSAGE).count() == 2
    assert _alerts(request, OperatorNotification.Kind.NEW_REQUEST).count() == 2
    assert not OperatorNotification.objects.filter(binding__provider="telegram").exists()


# --- Legacy operators' Telegram inbox ---------------------------------------------------


@console_on
def test_max_activity_is_not_repeated_by_the_telegram_operators_bot(staff):
    request = _request(build_part(), key="H" * 32, messenger=CustomerRequest.Messenger.MAX)
    worker = MaxBotWorker(FakeMaxApi(), worker_id="route-legacy", heartbeat_file="",
                          pacer=SendPacer(sleep=lambda s: None))
    from apps.customer_requests import max_service

    max_service.announce_new_requests(since=request.created_at)
    worker.dispatch_events()
    worker.dispatch_events()

    event = MaxOutboxEvent.objects.get(request=request)
    assert event.status == MaxOutboxEvent.Status.DISPATCHED
    assert not MaxOperatorDelivery.objects.exists()


@override_settings(CUSTOMER_OPERATOR_CONSOLE_ENABLED=False)
def test_without_the_console_the_legacy_inbox_still_announces_max_requests(staff):
    """No MAX staff channel exists then: the only alert is kept as before."""
    request = _request(build_part(), key="I" * 32, messenger=CustomerRequest.Messenger.MAX)
    worker = MaxBotWorker(FakeMaxApi(), worker_id="route-off", heartbeat_file="",
                          pacer=SendPacer(sleep=lambda s: None))
    from apps.customer_requests import max_service

    max_service.announce_new_requests(since=request.created_at)
    worker.dispatch_events()

    assert MaxOperatorDelivery.objects.filter(event__request=request).count() == 3
    assert not OperatorNotification.objects.exists()


# --- Replies still go to the customer's own messenger ------------------------------------


@console_on
def test_staff_in_either_messenger_still_answers_a_max_customer_in_max(staff):
    from apps.customer_requests import operator_replies

    request = _request(build_part(), key="J" * 32, messenger=CustomerRequest.Messenger.MAX)
    token = issue_max_link(request_id=request.pk).token
    consume_max_start(token=token, chat_id=MAX_CUSTOMER_CHAT, user_id=MAX_CUSTOMER_USER)

    result = operator_replies.submit_reply(
        request_id=request.pk, user=staff["tg"][0].user, text="Ответ из Telegram",
        key="b" * 32, channel=CustomerRequest.Messenger.MAX,
    )

    assert isinstance(result.message, MaxMessage)
    assert result.message.recipient_chat_id == MAX_CUSTOMER_CHAT


# --- Quantity in the staff request card (Telegram and MAX share it) ----------------------


def _card(request, provider, staff):
    binding = staff["tg"][0] if provider == "telegram" else staff["max"][0]
    return operator_console.card(request, binding=binding)[0]


@console_on
@pytest.mark.parametrize("provider", ["telegram", "max"])
@pytest.mark.parametrize(("stored", "shown"), [("1", "1"), ("2", "2"), ("15", "15")])
def test_request_card_shows_whole_quantities_without_decimals(staff, provider, stored, shown):
    request = _request(build_part(), key=f"{provider[0]}{stored}".ljust(32, "q"),
                       messenger=CustomerRequest.Messenger.TELEGRAM)
    CustomerRequestLine.objects.filter(request=request).update(
        quantity_requested=Decimal(stored)
    )
    line = CustomerRequestLine.objects.get(request=request)
    assert line.quantity_requested == Decimal(f"{stored}.000")  # stored with 3 places

    text = _card(CustomerRequest.objects.get(pk=request.pk), provider, staff)

    assert f"{line.part_name} · {shown} × " in text
    assert f"{stored}.000" not in text
    assert f"{stored},000" not in text


@console_on
@pytest.mark.parametrize("provider", ["telegram", "max"])
def test_request_card_never_truncates_a_fractional_quantity(staff, provider):
    """A repeat purchase of oil sold by the litre can ask for 2.5: it stays 2,5."""
    request = _request(build_part(), key=f"{provider}-frac".ljust(32, "z"),
                       messenger=CustomerRequest.Messenger.MAX)
    CustomerRequestLine.objects.filter(request=request).update(
        quantity_requested=Decimal("2.500")
    )

    text = _card(CustomerRequest.objects.get(pk=request.pk), provider, staff)

    assert " · 2,5 × " in text
    assert " · 2 × " not in text and "2.500" not in text
