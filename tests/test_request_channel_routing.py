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


# --- Quantity: pieces as whole numbers, oil liters as decimals --------------------------
#
# What a quantity measures comes from PartType.is_oil (apps.catalog.quantity_units),
# never from whether the Decimal happens to have a fractional part.


def _oil_part():
    from apps.catalog.models import Category, PartNumber, PartType, Unit

    part = PartType.objects.create(
        name="МАСЛО МОТОРНОЕ",
        category=Category.objects.get_or_create(name="Масла", parent=None)[0],
        unit=Unit.objects.get_or_create(name="Литр", defaults={"short_name": "л"})[0],
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True,
        oil_package_volume_l=Decimal("4"),
        recommended_price=Decimal("4000"),
    )
    PartNumber.objects.create(part=part, value="OIL-ROUTE-1", is_primary=True)
    return part


def _request_with(part, quantity, key):
    request = _request(build_part(), key=key, messenger=CustomerRequest.Messenger.TELEGRAM)
    CustomerRequestLine.objects.filter(request=request).update(
        part_type=part, quantity_requested=Decimal(quantity), part_name=part.name
    )
    return CustomerRequest.objects.get(pk=request.pk)


def _all_renderings(request, staff):
    """The staff card in Telegram and in MAX, the customer summary, the legacy card."""
    from apps.customer_requests import messaging, operator_bot

    return {
        "telegram card": operator_console.card(request, binding=staff["tg"][0])[0],
        "max card": operator_console.card(request, binding=staff["max"][0])[0],
        "customer summary": "\n".join(
            messaging.summary_line(line)[0] for line in request.lines.all()
        ),
        "legacy operator card": operator_bot.card_text(request),
    }


@pytest.mark.parametrize(
    ("stored", "shown"),
    [("1.000", "1"), ("2.000", "2"), ("15.000", "15")],
)
def test_piece_quantities_are_whole_numbers_everywhere(staff, stored, shown):
    part = build_part()
    request = _request_with(part, stored, key=f"piece-{stored}".ljust(32, "p"))
    assert CustomerRequestLine.objects.get(request=request).quantity_requested == Decimal(stored)

    for surface, text in _all_renderings(request, staff).items():
        assert f" {shown} " in f" {text} ".replace("\n", " "), surface
        for wrong in (stored, stored.replace(".", ","), f"{shown},0", f"{shown}.0"):
            assert wrong not in text, (surface, wrong)


@pytest.mark.parametrize(
    ("stored", "shown"),
    [("0.500", "0,5"), ("1.500", "1,5"), ("2.750", "2,75"), ("2.000", "2")],
)
def test_oil_liters_keep_their_decimals_without_trailing_zeros(staff, stored, shown):
    part = _oil_part()
    request = _request_with(part, stored, key=f"oil-{stored}".ljust(32, "o"))

    for surface, text in _all_renderings(request, staff).items():
        assert f" {shown} " in f" {text} ".replace("\n", " "), surface
        assert stored not in text and stored.replace(".", ",") not in text, surface


def test_a_fractional_piece_quantity_is_never_turned_into_a_wrong_whole_number(staff):
    """1.5 of a piece part is invalid data (see the report); it stays visible as 1,5."""
    part = build_part()
    request = _request_with(part, "1.500", key="piece-fraction".ljust(32, "f"))

    for surface, text in _all_renderings(request, staff).items():
        flat = f" {text} ".replace("\n", " ")
        assert " 1,5 " in flat, surface
        assert " 1 " not in flat and " 2 " not in flat and "1.500" not in flat, surface


def test_format_quantity_is_decided_by_the_part_not_by_the_number():
    from apps.catalog.models import PartType
    from apps.catalog.quantity_units import format_quantity

    piece = PartType(is_oil=False)
    oil = PartType(is_oil=True)
    assert format_quantity(Decimal("15.000"), piece) == "15"
    assert format_quantity(Decimal("1.000"), piece) == "1"
    assert format_quantity(Decimal("1.500"), oil) == "1,5"
    assert format_quantity(Decimal("2.750"), oil) == "2,75"
    assert format_quantity(Decimal("100.000"), oil) == "100"
    assert format_quantity(Decimal("1000.000"), piece) == "1000"
