"""The operators' Telegram bot as one workspace for MAX and Telegram customers.

Release A. The Bot API is the same ``FakeBotApi`` the Telegram suite uses; the
worker, services and models are production code. MAX customers are bound and
heard through the real MAX services, without any MAX network: what matters here
is what the employee sees and where their answer goes.
"""
import pytest
from django.test import override_settings
from django.urls import reverse

from apps.customer_requests import max_service, operator_bot, telegram_service
from apps.customer_requests.max_bot import MaxBotWorker
from apps.customer_requests.messengers import consume_max_start, issue_max_link
from apps.customer_requests.models import (
    CustomerRequest,
    MaxMessage,
    TelegramMessage,
    TelegramOperator,
)
from apps.customer_requests.services import change_request_status
from apps.customer_requests.telegram_bot import TelegramBotWorker

from .test_telegram_customer_messaging import (
    CUSTOMER,
    OPERATOR_A,
    OPERATOR_B,
    STRANGER,
    FakeBotApi,
    _operator,
    _request,
    build_part,
    callback_update,
    link,
    message_update,
    run,
)


@pytest.fixture
def part(db):
    return build_part()


@pytest.fixture
def api():
    return FakeBotApi()


@pytest.fixture
def worker(db, api):
    bot = TelegramBotWorker(api, worker_id="worker-ops", poll_timeout=0, heartbeat_file="")
    bot.start()
    return bot


@pytest.fixture
def operators(db, django_user_model):
    return (
        _operator(django_user_model, OPERATOR_A, username="denis"),
        _operator(django_user_model, OPERATOR_B, username="masha"),
    )

MAX_USER = 7110001
MAX_CHAT = 7110002
_mids = iter(f"mid.bot{number}" for number in range(1, 2000))


def max_request(part, *, key, user_id=MAX_USER, chat_id=MAX_CHAT):
    """A MAX request whose customer has started the bot, as the real link does."""
    request = _request(part, key=key, messenger=CustomerRequest.Messenger.MAX)
    token = issue_max_link(request_id=request.pk).token
    consume_max_start(token=token, chat_id=chat_id, user_id=user_id)
    return request


def max_customer_writes(text, *, user_id=MAX_USER, chat_id=MAX_CHAT):
    """The customer's MAX message, then MAX's own fan-out to the employees.

    The MAX worker only turns its outbox events into deliveries here; the
    operators' Telegram bot is what actually sends them.
    """
    result = max_service.record_customer_message(
        user_id=user_id, chat_id=chat_id, mid=next(_mids), text=text
    )
    MaxBotWorker(None, worker_id="max-events", heartbeat_file="").dispatch_events()
    return result


def buttons_of(item):
    markup = item["reply_markup"] or {"inline_keyboard": []}
    return [button for row in markup["inline_keyboard"] for button in row]


def reply_button(item):
    return next(
        (button for button in buttons_of(item) if button["text"] == "Ответить"), None
    )


def texts(api, chat_id):
    return api.texts_to(chat_id)


# --- Notifications ------------------------------------------------------------------------


def test_a_max_customer_message_reaches_employees_with_reply_and_open_buttons(
    part, worker, api, operators
):
    request = max_request(part, key="M" * 32)
    max_customer_writes("Можно доставку в субботу?")
    run(worker, api)

    notification = api.last_with(OPERATOR_A, "Новое сообщение клиента")
    assert f"Заявка №{request.reference} · MAX" in notification["text"]
    assert "Клиент: Иван Петров" in notification["text"]
    assert "«Можно доставку в субботу?»" in notification["text"]
    assert reply_button(notification)["callback_data"] == f"r:{request.public_id.hex}"
    assert any(b["text"] == "Активные заявки" for b in buttons_of(notification))


def test_a_telegram_customer_message_reaches_employees_the_same_way(
    part, worker, api, operators
):
    request = _request(part, key="T" * 32)
    link(worker, api, request)
    run(worker, api, message_update(CUSTOMER, "Когда забрать?"))

    notification = api.last_with(OPERATOR_A, "Новое сообщение клиента")
    assert f"Заявка №{request.reference} · Telegram" in notification["text"]
    assert "«Когда забрать?»" in notification["text"]
    assert reply_button(notification)["callback_data"] == f"r:{request.public_id.hex}"


@override_settings(TELEGRAM_INTERNAL_BASE_URL="https://denisstock.example")
def test_open_request_points_at_this_exact_request_in_denisstock(part, worker, api, operators):
    request = max_request(part, key="O" * 32)
    max_customer_writes("вопрос")
    run(worker, api)

    notification = api.last_with(OPERATOR_A, "Новое сообщение клиента")
    open_button = next(b for b in buttons_of(notification) if "url" in b)
    assert open_button["url"] == (
        f"https://denisstock.example{reverse('customer_request_detail', args=[request.pk])}"
    )
    assert "denisstock.example" not in "\n".join(texts(api, MAX_CHAT))


# --- Reply mode ---------------------------------------------------------------------------


def test_reply_mode_names_the_request_the_customer_and_the_last_message(
    part, worker, api, operators
):
    request = max_request(part, key="P" * 32)
    max_customer_writes("как проходит оплата?")
    run(worker, api)

    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))

    prompt = api.last_with(OPERATOR_A, "Ответ на заявку")
    assert f"Ответ на заявку №{request.reference} · MAX" in prompt["text"]
    assert "Клиент: Иван Петров" in prompt["text"]
    assert "«как проходит оплата?»" in prompt["text"]
    assert {b["text"] for b in buttons_of(prompt)} == {"Отмена", "К заявкам"}


def test_an_employee_answers_a_max_customer_from_the_bot(part, worker, api, operators):
    request = max_request(part, key="Q" * 32)
    max_customer_writes("Есть в наличии?")
    run(worker, api)

    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))
    run(worker, api, message_update(OPERATOR_A, "Есть, ждём вас сегодня."))

    reply = MaxMessage.objects.get(direction=MaxMessage.Direction.OPERATOR)
    assert reply.text == "Есть, ждём вас сегодня."
    assert reply.conversation.request_id == request.pk
    assert reply.operator_user == operators[0].user
    assert reply.recipient_chat_id == MAX_CHAT
    assert api.last_with(OPERATOR_A, "поставлен в отправку")["text"].startswith(
        f"Ответ по заявке №{request.reference}"
    )
    # Reply mode is over: the next text is not a reply to anybody.
    assert TelegramOperator.objects.get(pk=operators[0].pk).reply_request is None


def test_an_employee_answers_a_telegram_customer_from_the_bot(part, worker, api, operators):
    request = _request(part, key="R" * 32)
    link(worker, api, request)
    run(worker, api, message_update(CUSTOMER, "Когда забрать?"))

    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))
    run(worker, api, message_update(OPERATOR_A, "Сегодня после 15:00."))

    assert texts(api, CUSTOMER)[-1] == "Сегодня после 15:00."
    reply = TelegramMessage.objects.get(direction=TelegramMessage.Direction.OPERATOR)
    assert reply.conversation.request_id == request.pk
    assert reply.operator_user == operators[0].user


# --- Several requests at once ---------------------------------------------------------------


def test_a_notification_about_b_never_moves_a_reply_aimed_at_a(part, worker, api, operators):
    first = max_request(part, key="A" * 32)
    second = _request(part, key="B" * 32)
    link(worker, api, second)
    max_customer_writes("вопрос по первой")
    run(worker, api)

    # The employee aims at A...
    run(worker, api, callback_update(OPERATOR_A, f"r:{first.public_id.hex}"))
    # ...and B's customer writes while they are typing.
    run(worker, api, message_update(CUSTOMER, "вопрос по второй"))
    assert api.last_with(OPERATOR_A, "вопрос по второй")
    assert TelegramOperator.objects.get(pk=operators[0].pk).reply_request_id == first.pk

    run(worker, api, message_update(OPERATOR_A, "ответ по первой"))

    assert MaxMessage.objects.get(text="ответ по первой").conversation.request_id == first.pk
    assert not TelegramMessage.objects.filter(
        direction=TelegramMessage.Direction.OPERATOR
    ).exists()

    # Only an explicit choice moves the target.
    run(worker, api, callback_update(OPERATOR_A, f"r:{second.public_id.hex}"))
    run(worker, api, message_update(OPERATOR_A, "ответ по второй"))

    answered = TelegramMessage.objects.get(text="ответ по второй")
    assert answered.conversation.request_id == second.pk
    assert MaxMessage.objects.filter(direction=MaxMessage.Direction.OPERATOR).count() == 1


def test_two_employees_answer_their_own_requests_without_crossing(part, worker, api, operators):
    first = max_request(part, key="C" * 32)
    second = _request(part, key="D" * 32)
    link(worker, api, second)

    run(worker, api, callback_update(OPERATOR_A, f"r:{first.public_id.hex}"))
    run(worker, api, callback_update(OPERATOR_B, f"r:{second.public_id.hex}"))
    run(worker, api, message_update(OPERATOR_B, "от Маши по второй"))
    run(worker, api, message_update(OPERATOR_A, "от Дениса по первой"))

    assert TelegramMessage.objects.get(text="от Маши по второй").operator_user == operators[1].user
    assert MaxMessage.objects.get(text="от Дениса по первой").operator_user == operators[0].user


# --- Stale buttons and duplicates -------------------------------------------------------------


def test_a_button_for_a_request_closed_since_refuses_reply_mode(
    part, worker, api, operators, admin_user
):
    request = max_request(part, key="E" * 32)
    max_customer_writes("вопрос")
    run(worker, api)
    change_request_status(request_id=request.pk, target_status="canceled", by=admin_user)

    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))

    answer = api.last_with(OPERATOR_A, "уже закрыта")
    assert f"Заявка №{request.reference} уже закрыта." in answer["text"]
    assert reply_button(answer) is None
    assert any(b["text"] == "Активные заявки" for b in buttons_of(answer))
    assert TelegramOperator.objects.get(pk=operators[0].pk).reply_request is None

    run(worker, api, message_update(OPERATOR_A, "поздний ответ"))
    assert not MaxMessage.objects.filter(direction=MaxMessage.Direction.OPERATOR).exists()


def test_a_request_closed_while_the_employee_was_typing_sends_nothing(
    part, worker, api, operators, admin_user
):
    request = max_request(part, key="F" * 32)
    max_customer_writes("вопрос")
    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))
    change_request_status(request_id=request.pk, target_status="canceled", by=admin_user)

    run(worker, api, message_update(OPERATOR_A, "ответ после закрытия"))

    assert not MaxMessage.objects.filter(direction=MaxMessage.Direction.OPERATOR).exists()
    assert api.last_with(OPERATOR_A, "уже закрыта")


def test_pressing_reply_twice_and_a_replayed_update_stay_safe(part, worker, api, operators):
    request = max_request(part, key="G" * 32)
    max_customer_writes("вопрос")

    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))
    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))
    update = message_update(OPERATOR_A, "один ответ")
    run(worker, api, update)
    from apps.customer_requests.telegram_bot import handle_update

    handle_update(update)  # a replayed update

    assert MaxMessage.objects.filter(direction=MaxMessage.Direction.OPERATOR).count() == 1


def test_a_stale_button_of_a_deleted_request_says_so_without_leaking(part, worker, api, operators):
    run(worker, api, callback_update(OPERATOR_A, "r:" + "0" * 32))
    assert "Заявка не найдена." in texts(api, OPERATOR_A)


# --- The list -----------------------------------------------------------------------------


def test_the_active_list_holds_both_messengers_with_waiting_first(
    part, worker, api, operators, admin_user
):
    waiting = max_request(part, key="H" * 32)
    max_customer_writes("жду ответа")
    quiet = _request(part, key="I" * 32)
    link(worker, api, quiet)
    closed = _request(part, key="J" * 32)
    change_request_status(request_id=closed.pk, target_status="canceled", by=admin_user)

    run(worker, api, message_update(OPERATOR_A, "/requests"))

    listing = api.last_with(OPERATOR_A, "Активные заявки")
    rows = [button["text"] for button in buttons_of(listing)]
    assert rows[0].startswith(f"● №{waiting.reference} · MAX")
    assert "ждёт ответа" in rows[0]
    assert any(quiet.reference in row and "Telegram" in row for row in rows)
    assert not any(closed.reference in row for row in rows)
    assert "ждут ответа: 1" in listing["text"]


def test_a_card_opened_from_the_list_offers_the_reply(part, worker, api, operators):
    request = max_request(part, key="K" * 32)
    max_customer_writes("вопрос")
    run(worker, api, message_update(OPERATOR_A, "/requests"))
    listing = api.last_with(OPERATOR_A, "Активные заявки")
    card_button = buttons_of(listing)[0]

    run(worker, api, callback_update(OPERATOR_A, card_button["callback_data"]))

    card = api.last_with(OPERATOR_A, "ЗАЯВКА")
    assert card["text"].startswith(f"ЗАЯВКА №{request.reference} · MAX")
    assert "Связь: MAX, подключён" in card["text"]
    assert reply_button(card)["callback_data"] == f"r:{request.public_id.hex}"


def test_an_old_button_naming_a_telegram_conversation_still_opens_its_request(
    part, worker, api, operators
):
    """Buttons sent before this release name the conversation, not the request."""
    request = _request(part, key="L" * 32)
    link(worker, api, request)
    conversation = request.telegram_conversation

    run(worker, api, callback_update(OPERATOR_A, f"c:{conversation.public_id.hex}"))

    assert any(
        text.startswith(f"ЗАЯВКА №{request.reference}") for text in texts(api, OPERATOR_A)
    )


# --- Authorization -------------------------------------------------------------------------


def test_a_stranger_gets_nothing_from_any_operator_button(part, worker, api, operators):
    request = max_request(part, key="N" * 32)
    max_customer_writes("секретный вопрос")

    run(
        worker,
        api,
        callback_update(STRANGER, f"r:{request.public_id.hex}"),
        callback_update(STRANGER, f"c:{request.public_id.hex}"),
        message_update(STRANGER, "/requests"),
    )

    stranger_text = "\n".join(texts(api, STRANGER))
    for secret in (request.reference, "Иван", "секретный вопрос", "912"):
        assert secret not in stranger_text
    assert [text for _id, text in api.answers] == ["Недоступно."] * 2
    assert TelegramOperator.objects.filter(reply_request=request).count() == 0


def test_a_disabled_employee_can_no_longer_reply(part, worker, api, operators):
    request = max_request(part, key="S" * 32)
    max_customer_writes("вопрос")
    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))
    TelegramOperator.objects.filter(pk=operators[0].pk).update(is_active=False)

    run(worker, api, message_update(OPERATOR_A, "ответ уволенного"))

    assert not MaxMessage.objects.filter(text="ответ уволенного").exists()
    assert api.answers or "Недоступно." in "\n".join(texts(api, OPERATOR_A))


def test_the_customer_never_learns_who_answered(part, worker, api, operators):
    request = max_request(part, key="U" * 32)
    max_customer_writes("вопрос")
    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))
    run(worker, api, message_update(OPERATOR_A, "Ответ клиенту"))

    outgoing = MaxMessage.objects.get(direction=MaxMessage.Direction.OPERATOR)
    assert outgoing.text == "Ответ клиенту"
    for secret in ("denis", str(OPERATOR_A)):
        assert secret not in outgoing.text
    assert operator_bot.display_name(operators[0].user) == "denis"  # employees see it


def test_the_bot_menu_mentions_both_messengers(part, worker, api, operators):
    run(worker, api, message_update(OPERATOR_A, "/menu"))
    menu = api.last_with(OPERATOR_A, "Панель администратора PRO-STORE")
    assert "Клиентское меню отключено" not in menu["text"]
    assert menu["reply_markup"]["keyboard"] == [
        [{"text": "Все заявки"}],
        [{"text": "Новые заявки"}],
        [{"text": "Загрузка фото по продажам/ремонтам"}],
    ]


def test_cancel_returns_the_employee_to_the_list_without_sending(part, worker, api, operators):
    request = max_request(part, key="V" * 32)
    run(worker, api, callback_update(OPERATOR_A, f"r:{request.public_id.hex}"))
    run(worker, api, callback_update(OPERATOR_A, "x"))
    run(worker, api, message_update(OPERATOR_A, "это уже не ответ"))

    assert not MaxMessage.objects.filter(text="это уже не ответ").exists()
    assert "Ответ отменён." in texts(api, OPERATOR_A)
    assert telegram_service.OPERATOR_HELP_TEXT == (
        "Панель администратора PRO-STORE\n"
        "Доступно:\n"
        "- просмотр и общение по всем и новым заявкам;\n"
        "- загрузка фото по продажам/ремонтам.\n\n"
        "Кнопки находятся в меню рядом с полем ввода сообщения."
    )
