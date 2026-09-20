"""Release B: a customer navigates by buttons, in MAX and in Telegram alike.

Nobody here types a command unless the test is about backward compatibility.
The rules are the shared ones (``customer_ui``), so both transports are checked
against the same expectations: the greeting of a new request, «Мои заявки»,
which request is chosen, and what happens when the chosen one closes.
"""
import re
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.customer_requests import customer_ui, max_service, messaging, telegram_service
from apps.customer_requests.messengers import (
    consume_max_start,
    consume_telegram_start,
    issue_max_link,
    issue_telegram_link,
)
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestLine,
    CustomerRequestMessengerLinkToken,
    MaxCustomerChat,
    MaxMessage,
    TelegramCustomerChat,
    TelegramMessage,
)
from apps.customer_requests.services import (
    RequestLineInput,
    change_request_status,
    create_customer_request,
)

from .test_customer_requests import POLICY
from .test_telegram_customer_messaging import build_part

CHAT = 4100001
MAX_USER = 4200001
MAX_CHAT = 4200002
_keys = iter(f"{number:032x}" for number in range(3000, 8000))
_updates = iter(range(600_001, 699_999))
_mids = iter(f"mid.nav{number}" for number in range(1, 3000))


@pytest.fixture
def part(db):
    return build_part()


def make_request(part, *, messenger, priced=True, quantity="2"):
    request, _created = create_customer_request(
        customer_name="Иван Петров",
        customer_phone="+7 (912) 123-45-67",
        preferred_messenger=messenger,
        comment="",
        lines=[RequestLineInput(part_id=part.pk, quantity=quantity, supply_inquiry=True)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=next(_keys),
    )
    if not priced:
        request.lines.update(price_seen=None)
    return request


def link(request, *, chat=CHAT, user=MAX_USER, max_chat=MAX_CHAT):
    if request.preferred_messenger == CustomerRequest.Messenger.MAX:
        return consume_max_start(
            token=issue_max_link(request_id=request.pk).token, chat_id=max_chat, user_id=user
        )
    return consume_telegram_start(
        token=issue_telegram_link(request_id=request.pk).token,
        chat_id=chat,
        user_id=chat,
        username="ivan",
    )


# --- The greeting of a newly linked request ------------------------------------------------


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_a_new_request_is_greeted_by_name_with_its_own_summary(part, messenger):
    request = make_request(part, messenger=messenger)
    link(request)

    if messenger == CustomerRequest.Messenger.MAX:
        texts = list(
            MaxMessage.objects.filter(conversation__request=request).values_list("text", flat=True)
        )
    else:
        texts = list(
            TelegramMessage.objects.filter(conversation__request=request).values_list(
                "text", flat=True
            )
        )
    greeting = texts[0]
    assert greeting.startswith(f"Добрый день! Ваша заявка №{request.reference} получена.")
    assert "Ваш заказ:" in greeting
    assert "448 — РЕМЕНЬ ПРИВОДНОЙ" in greeting
    assert "2 шт. × 10 000 ₽ = 20 000 ₽" in greeting
    assert "Итого: 20 000 ₽" in greeting
    assert greeting.endswith(
        "Если у вас есть вопросы по заявке, напишите нам здесь — менеджер ответит вам."
    )
    # Nothing internal reaches the customer.
    conversation = getattr(request, f"{messenger}_conversation")
    for secret in ("принята", "token", "conversation", "callback", conversation.public_id.hex):
        assert secret not in greeting


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_an_unknown_price_is_never_greeted_as_zero(part, messenger):
    request = make_request(part, messenger=messenger, priced=False)
    link(request)

    model = MaxMessage if messenger == CustomerRequest.Messenger.MAX else TelegramMessage
    greeting = model.objects.filter(conversation__request=request).first().text
    assert "Итого: 0 ₽" not in greeting
    assert not re.search(r"(?<![\d\s])0 ₽", greeting)
    assert "цена уточняется" in greeting
    assert "Есть позиции, цена которых уточняется." in greeting


def test_the_greeting_is_written_once_per_request_whatever_is_redelivered(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)
    before = MaxMessage.objects.filter(conversation__request=request).count()
    assert before  # the greeting arrived once
    token_id = CustomerRequestMessengerLinkToken.objects.get(request=request).pk

    # The same handoff delivered again greets nobody a second time.
    for _ in range(2):
        max_service.bind_customer_chat(
            request=request, chat_id=MAX_CHAT, user_id=MAX_USER, link_token_id=token_id
        )

    assert MaxMessage.objects.filter(conversation__request=request).count() == before
    greetings = [
        text
        for text in MaxMessage.objects.filter(conversation__request=request).values_list(
            "text", flat=True
        )
        if text.startswith("Добрый день!")
    ]
    assert len(greetings) == 1


# --- «Мои заявки» -------------------------------------------------------------------------


def test_the_selector_speaks_plainly_for_none_one_and_many(part):
    empty = customer_ui.selector_view([], active_id=None, linked_any=True)
    assert empty.text == messaging.NO_OPEN_REQUESTS_TEXT
    assert not empty.has_choices

    first = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(first)
    one = telegram_service.selector_result(CHAT)
    assert one.reply.startswith("Ваша активная заявка:")
    assert f"✓ №{first.reference}" in one.reply
    assert "1 позиция" in one.reply
    assert "20 000 ₽" in one.reply

    second = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM, priced=False)
    link(second)
    many = telegram_service.selector_result(CHAT)
    assert many.reply.startswith("Мои активные заявки:")
    assert f"✓ №{second.reference}" in many.reply  # the newly linked one is current
    assert f"№{first.reference}" in many.reply
    assert "Цена уточняется" in many.reply
    labels = [row[0]["text"] for row in many.keyboard["inline_keyboard"]]
    assert labels == [f"✓ №{second.reference}", f"№{first.reference}"]
    assert not re.search(r"(?<![\d\s])0 ₽", many.reply)


def test_a_customer_with_nothing_open_is_told_so_without_dead_buttons(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(request)
    change_request_status(request_id=request.pk, target_status="canceled", by=None)

    result = telegram_service.selector_result(CHAT)

    assert result.reply == messaging.NO_OPEN_REQUESTS_TEXT
    assert not (result.keyboard or {}).get("inline_keyboard")


def test_the_telegram_keyboard_offers_my_requests_without_a_command(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(request)

    greeting = telegram_service.customer_greeting(CHAT)

    assert greeting.keyboard == telegram_service.customer_keyboard()
    assert greeting.keyboard["keyboard"][0][0]["text"] == "Мои заявки"
    assert greeting.keyboard["is_persistent"] is True
    assert "/requests" not in greeting.reply
    assert request.reference not in greeting.reply  # a hello, not a summary


def test_max_offers_my_requests_as_a_button_under_the_bots_own_message(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)
    MaxMessage.objects.all().delete()

    max_service.queue_greeting(user_id=MAX_USER, chat_id=MAX_CHAT, dedupe_key="greet:1")

    message = MaxMessage.objects.get()
    assert message.buttons == [[{"text": "Мои заявки", "payload": "menu"}]]
    assert "/requests" not in message.text
    assert max_service.is_menu_payload("menu") and not max_service.is_menu_payload("s:abc")


# --- Choosing between several requests ------------------------------------------------------


def test_opening_the_selector_never_changes_what_is_chosen(part):
    first = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    second = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(first)
    link(second)
    chosen = TelegramCustomerChat.objects.get(chat_id=CHAT).active_conversation

    telegram_service.selector_result(CHAT)
    telegram_service.customer_conversations_prompt(CHAT)

    assert TelegramCustomerChat.objects.get(chat_id=CHAT).active_conversation == chosen


def test_a_new_request_does_not_silently_steal_an_explicit_choice(part):
    first = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    second = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(first)
    link(second)
    # The customer goes back to the first one on purpose.
    telegram_service.select_customer_conversation(
        chat_id=CHAT, conversation_hex=first.telegram_conversation.public_id.hex
    )
    telegram_service.record_customer_message(
        chat_id=CHAT, update_id=next(_updates), text="про первую"
    )

    assert TelegramMessage.objects.get(text="про первую").conversation.request_id == first.pk
    view = telegram_service.selector_result(CHAT)
    assert f"✓ №{first.reference}" in view.reply


def test_an_operator_reply_on_another_request_does_not_move_the_customers_choice(
    part, django_user_model
):
    from apps.customer_requests import operator_replies

    seller = django_user_model.objects.create_superuser(username="boss-b", password="x" * 12)
    first = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    second = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(first)
    link(second)
    telegram_service.select_customer_conversation(
        chat_id=CHAT, conversation_hex=first.telegram_conversation.public_id.hex
    )

    operator_replies.submit_reply(
        request_id=second.pk, user=seller, text="ответ по второй", key="c" * 32
    )

    assert TelegramCustomerChat.objects.get(chat_id=CHAT).active_conversation.request_id == (
        first.pk
    )
    telegram_service.record_customer_message(
        chat_id=CHAT, update_id=next(_updates), text="всё ещё первая"
    )
    assert TelegramMessage.objects.get(text="всё ещё первая").conversation.request_id == first.pk


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_switching_confirms_in_one_line_and_never_repeats_the_greeting(part, messenger):
    first = make_request(part, messenger=messenger)
    second = make_request(part, messenger=messenger)
    link(first)
    link(second)

    if messenger == CustomerRequest.Messenger.MAX:
        max_service.select_customer_conversation(
            user_id=MAX_USER,
            chat_id=MAX_CHAT,
            payload=f"s:{first.max_conversation.public_id.hex}",
            callback_id="cb-1",
            press_key="press-1",
        )
        texts = list(MaxMessage.objects.order_by("pk").values_list("text", flat=True))
        assert f"Выбрана заявка №{first.reference}." in texts
    else:
        conversation = telegram_service.select_customer_conversation(
            chat_id=CHAT, conversation_hex=first.telegram_conversation.public_id.hex
        )
        assert conversation.request_id == first.pk
        assert customer_ui.selected_text(first.reference) == f"Выбрана заявка №{first.reference}."
        texts = list(TelegramMessage.objects.values_list("text", flat=True))
    # The whole greeting belongs to a new request, not to switching.
    assert sum(text.startswith("Добрый день!") for text in texts) == 2


def test_messages_follow_the_chosen_request_and_never_cross(part):
    first = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    second = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(first)
    link(second)

    telegram_service.record_customer_message(chat_id=CHAT, update_id=next(_updates), text="во B")
    telegram_service.select_customer_conversation(
        chat_id=CHAT, conversation_hex=first.telegram_conversation.public_id.hex
    )
    telegram_service.record_customer_message(chat_id=CHAT, update_id=next(_updates), text="во A")

    assert TelegramMessage.objects.get(text="во B").conversation.request_id == second.pk
    assert TelegramMessage.objects.get(text="во A").conversation.request_id == first.pk


# --- Closed requests and stale buttons --------------------------------------------------------


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_a_request_closed_while_chosen_is_reported_and_never_swapped(part, messenger):
    chosen = make_request(part, messenger=messenger)
    other = make_request(part, messenger=messenger)
    link(chosen)
    link(other)
    if messenger == CustomerRequest.Messenger.MAX:
        max_service.select_customer_conversation(
            user_id=MAX_USER, chat_id=MAX_CHAT,
            payload=f"s:{chosen.max_conversation.public_id.hex}",
            callback_id="cb-2", press_key="press-2",
        )
    else:
        telegram_service.select_customer_conversation(
            chat_id=CHAT, conversation_hex=chosen.telegram_conversation.public_id.hex
        )
    change_request_status(request_id=chosen.pk, target_status="canceled", by=None)

    if messenger == CustomerRequest.Messenger.MAX:
        outcome = max_service.record_customer_message(
            user_id=MAX_USER, chat_id=MAX_CHAT, mid=next(_mids), text="а что с заявкой?"
        )
        assert outcome == max_service.CLOSED
        reply = MaxMessage.objects.order_by("-pk").first().text
        stored = MaxMessage.objects.filter(text="а что с заявкой?").count()
        routing = MaxCustomerChat.objects.get(user_id=MAX_USER).active_conversation
    else:
        result = telegram_service.record_customer_message(
            chat_id=CHAT, update_id=next(_updates), text="а что с заявкой?"
        )
        reply = result.reply
        stored = TelegramMessage.objects.filter(text="а что с заявкой?").count()
        routing = TelegramCustomerChat.objects.get(chat_id=CHAT).active_conversation

    assert reply.startswith(f"Заявка №{chosen.reference} уже закрыта.")
    assert stored == 0
    assert routing.request_id == chosen.pk  # never silently moved to the other one


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_a_stale_button_is_safe_however_often_it_is_pressed(part, messenger):
    closed = make_request(part, messenger=messenger)
    open_one = make_request(part, messenger=messenger)
    link(closed)
    link(open_one)
    change_request_status(request_id=closed.pk, target_status="canceled", by=None)

    for press in ("press-a", "press-b"):
        if messenger == CustomerRequest.Messenger.MAX:
            result = max_service.select_customer_conversation(
                user_id=MAX_USER, chat_id=MAX_CHAT,
                payload=f"s:{closed.max_conversation.public_id.hex}",
                callback_id="cb", press_key=press,
            )
        else:
            result = telegram_service.select_customer_conversation(
                chat_id=CHAT, conversation_hex=closed.telegram_conversation.public_id.hex
            )
        assert result is None

    if messenger == CustomerRequest.Messenger.MAX:
        routing = MaxCustomerChat.objects.get(user_id=MAX_USER).active_conversation
    else:
        routing = TelegramCustomerChat.objects.get(chat_id=CHAT).active_conversation
    assert routing.request_id == open_one.pk  # unchanged by the stale presses
    assert CustomerRequest.objects.get(pk=closed.pk).status == "canceled"


def test_the_selector_never_offers_a_closed_request(part):
    closed = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    open_one = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(closed)
    link(open_one)
    change_request_status(request_id=closed.pk, target_status="canceled", by=None)

    view = telegram_service.selector_result(CHAT)

    assert f"✓ №{closed.human_number}" not in view.reply
    assert f"№{open_one.reference}" in view.reply
    payloads = [row[0]["callback_data"] for row in view.keyboard["inline_keyboard"]]
    assert payloads == [f"s:{open_one.telegram_conversation.public_id.hex}"]


# --- Ownership ------------------------------------------------------------------------------


def test_another_customers_request_can_never_be_selected_or_written_to(part):
    mine = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    theirs = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(mine, chat=CHAT)
    link(theirs, chat=CHAT + 99)

    assert (
        telegram_service.select_customer_conversation(
            chat_id=CHAT, conversation_hex=theirs.telegram_conversation.public_id.hex
        )
        is None
    )
    assert telegram_service.closed_selection(
        chat_id=CHAT, conversation_hex=theirs.telegram_conversation.public_id.hex
    ) is None
    view = telegram_service.selector_result(CHAT)
    assert f"№{theirs.human_number}" not in view.reply


def test_a_max_request_cannot_be_selected_by_a_different_max_user(part):
    mine = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(mine)

    stolen = max_service.select_customer_conversation(
        user_id=MAX_USER + 5,
        chat_id=MAX_CHAT + 5,
        payload=f"s:{mine.max_conversation.public_id.hex}",
        callback_id="cb-x",
        press_key="press-x",
    )

    assert stolen is None
    assert MaxCustomerChat.objects.get(user_id=MAX_USER).active_conversation.request_id == mine.pk


@pytest.mark.parametrize("payload", ["s:" + "0" * 32, "s:zzz", "s:", "menu:evil", "", "s:%s"])
def test_a_forged_payload_selects_nothing(part, payload):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)

    result = max_service.select_customer_conversation(
        user_id=MAX_USER, chat_id=MAX_CHAT, payload=payload, callback_id="cb", press_key="p"
    )

    assert result is None
    assert MaxCustomerChat.objects.get(user_id=MAX_USER).active_conversation.request_id == (
        request.pk
    )


# --- Acknowledgement ---------------------------------------------------------------------------


def test_navigation_messages_never_count_as_customer_messages_or_acks(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(request)

    telegram_service.selector_result(CHAT)
    first = telegram_service.record_customer_message(
        chat_id=CHAT, update_id=next(_updates), text="первое"
    )
    telegram_service.selector_result(CHAT)
    second = telegram_service.record_customer_message(
        chat_id=CHAT, update_id=next(_updates), text="второе"
    )

    assert first.reply == messaging.CUSTOMER_ACK_TEXT
    assert second.reply == ""  # the acknowledgement stays once per request
    assert TelegramMessage.objects.filter(direction="customer_to_operator").count() == 2


def test_the_my_requests_button_text_is_not_stored_as_a_customer_message(part):
    from apps.customer_requests.telegram_bot import handle_update

    request = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(request)

    outgoing = handle_update(
        {
            "update_id": next(_updates),
            "message": {
                "message_id": 5,
                "chat": {"id": CHAT, "type": "private"},
                "from": {"id": CHAT, "is_bot": False},
                "text": "Мои заявки",
            },
        }
    )

    assert outgoing and "активная заявка" in outgoing[0].text
    assert not TelegramMessage.objects.filter(text="Мои заявки").exists()


# --- Money and plurals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 позиция"), (2, "2 позиции"), (5, "5 позиций"), (11, "11 позиций"), (21, "21 позиция")],
)
def test_positions_are_counted_in_russian(count, expected):
    assert customer_ui.positions_text(count) == expected


def test_a_partly_priced_request_shows_what_is_known_and_says_the_rest_is_pending(part):
    from apps.catalog.models import PartType

    request = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    other_part = PartType.objects.create(
        name="ВТОРАЯ ДЕТАЛЬ",
        category=part.category,
        unit=part.unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    CustomerRequestLine.objects.create(
        request=request,
        part_type=other_part,
        quantity_requested=Decimal("1"),
        unit_name="Штука",
        unit_short_name="шт",
        price_seen=None,
        article="X-1",
        part_name="ВТОРАЯ ДЕТАЛЬ",
    )

    money = customer_ui.request_money(request)

    assert money.text() == "20 000 ₽ · цена части позиций уточняется"
    assert "0 ₽" != money.text()


def test_an_unpriced_request_says_the_price_is_being_clarified(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM, priced=False)

    assert customer_ui.request_money(request).text() == "Цена уточняется"


def test_commands_still_work_for_anyone_who_learned_them(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.TELEGRAM)
    link(request)

    result = telegram_service.customer_conversations_prompt(CHAT)

    assert f"№{request.reference}" in result.reply
    assert re.search(r"✓ №\d+", result.reply)
    assert timezone.now() is not None


# --- MAX: the way in rides on the handoff greeting -----------------------------------------


def _max_summary_rows(request):
    return list(
        MaxMessage.objects.filter(
            conversation__request=request, dedupe_key__startswith="summary:"
        ).order_by("pk")
    )


def test_the_max_handoff_greeting_itself_offers_my_requests(part):
    """A MAX customer who followed the real deep link needs no command at all.

    MAX has no persistent keyboard, so «Мои заявки» has to arrive attached to
    something. It rides on the last summary message of the handoff.
    """
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)

    rows = _max_summary_rows(request)

    assert rows, "the handoff must greet"
    assert rows[-1].buttons == max_service.menu_button()
    assert customer_ui.MY_REQUESTS_BUTTON in str(rows[-1].buttons)
    assert [row.buttons for row in rows[:-1]] == [None] * (len(rows) - 1)


def test_the_max_handoff_button_opens_the_selector_without_a_command(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)
    payload = _max_summary_rows(request)[-1].buttons[0][0]["payload"]

    assert max_service.is_menu_payload(payload)

    view, buttons = max_service.selector_view(MAX_USER)

    assert f"№{request.reference}" in view.text
    assert buttons and f"{customer_ui.CURRENT_MARK} №{request.reference}" == buttons[0][0]["text"]


def test_a_returning_max_customer_gets_the_button_on_the_new_request_too(part):
    first = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(first)
    second = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(second)

    assert _max_summary_rows(second)[-1].buttons == max_service.menu_button()
    # The newly linked request is current; the older one stays reachable.
    view, buttons = max_service.selector_view(MAX_USER)
    labels = [button[0]["text"] for button in buttons]
    assert f"{customer_ui.CURRENT_MARK} №{second.reference}" in labels
    assert f"№{first.reference}" in labels


def test_the_handoff_button_costs_no_extra_message_and_survives_a_replay(part):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)
    # The real token id, never a hard-coded one: ids only line up by accident.
    token_row = CustomerRequestMessengerLinkToken.objects.get(request=request)
    before = list(
        MaxMessage.objects.filter(conversation__request=request).values_list("pk", "dedupe_key")
    )

    # A redelivered binding of the very same token adds nothing.
    max_service.bind_customer_chat(
        request=request, chat_id=MAX_CHAT, user_id=MAX_USER, link_token_id=token_row.pk
    )

    after = list(
        MaxMessage.objects.filter(conversation__request=request).values_list("pk", "dedupe_key")
    )
    greetings = [row for row in after if "получена" in MaxMessage.objects.get(pk=row[0]).text]

    assert after == before
    assert len(greetings) == 1
    assert _max_summary_rows(request)[-1].buttons == max_service.menu_button()
