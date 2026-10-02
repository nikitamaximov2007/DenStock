"""Bot worker reliability: one bad unit fails alone, visibly, and never as sent.

Telegram and MAX workers alike: an update, an outgoing row or a whole cycle
that raises something unexpected must not stop the worker, must not take the
units around it down with it, and must never end as ``sent``. A row that may
have reached the messenger is ``uncertain`` and is never sent again. Logs
show where the failure happened, never the failing message's own text.
"""

import logging
import socket
import threading
import urllib.request
from types import SimpleNamespace
from unittest import mock

import pytest
from django.db import OperationalError, connection
from django.utils import timezone

from apps.core.observability import exception_trace
from apps.customer_requests import max_bot, operator_console, telegram_bot
from apps.customer_requests import telegram_service as telegram_service_module
from apps.customer_requests.max_api import MaxBotApi, MaxNetworkError
from apps.customer_requests.max_bot import MaxBotWorker, SendPacer
from apps.customer_requests.models import (
    MaxDeliveryStatus,
    MaxMessage,
    TelegramConversation,
    TelegramDelivery,
    TelegramDeliveryStatus,
    TelegramMessage,
)
from apps.customer_requests.telegram_api import TelegramBotApi, TelegramNetworkError
from apps.customer_requests.telegram_bot import TelegramBotWorker
from apps.operations.models import MaxBotRuntime, TelegramBotRuntime

from .max_fake import (
    FAKE_MAX_TOKEN,
    FAKE_WEBHOOK_SECRET,
    FakeMaxServer,
    deliver,
    message_created,
)
from .test_telegram_customer_messaging import (
    OPERATOR_A,
    OPERATOR_B,
    FakeBotApi,
    _operator,
    _request,
    build_part,
    link,
    message_update,
)

CHAT = 52_000_001
OTHER_CHAT = 52_000_002
SECRET_TEXT = "секретный текст клиента 4111"
TG_FAKE_TOKEN = "123456789:AAFakeTokenForReliabilityTests_abcdefghij"


@pytest.fixture(autouse=True)
def bot_settings(settings):
    settings.CUSTOMER_MESSENGER_CABINET_ENABLED = True
    settings.MAX_WEBHOOK_ENABLED = True
    settings.MAX_WEBHOOK_SECRET = FAKE_WEBHOOK_SECRET
    settings.MAX_BOT_USERNAME = "id0000000000_bot"
    settings.MAX_DEEP_LINK_BASE_URL = "https://max.ru"
    settings.TELEGRAM_INTERNAL_BASE_URL = "https://denstock.example"
    return settings


@pytest.fixture
def no_pause(monkeypatch):
    """The worker loop's back-off waits and connection resets are not under test."""
    monkeypatch.setattr(connection, "close", lambda: None)


# --- MAX ----------------------------------------------------------------------------------


@pytest.fixture
def server():
    fake = FakeMaxServer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def max_worker(db, server, no_pause):
    api = MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=2)
    worker = MaxBotWorker(
        api, worker_id="max-rel", heartbeat_file="", pacer=SendPacer(sleep=lambda s: None)
    )
    worker.stop.wait = lambda seconds=None: False
    return worker


def _max_row(text, *, chat=CHAT, buttons=None):
    return MaxMessage.objects.create(
        direction=MaxMessage.Direction.SYSTEM,
        text=text,
        buttons=buttons,
        delivery_status=MaxDeliveryStatus.PENDING,
        recipient_chat_id=chat,
        dedupe_key=f"reliability:{text}",
        next_attempt_at=timezone.now(),
    )


def _status(row):
    row.refresh_from_db()
    return row.delivery_status


VALID_BUTTONS = [[{"text": "Мои заявки", "payload": "r"}]]
# A button without its payload: building the MAX keyboard raises KeyError.
MALFORMED_BUTTONS = [[{"text": "Сломанная кнопка"}]]


def test_max_poison_row_fails_alone_and_the_rows_around_it_are_sent(
    max_worker, server, caplog
):
    first = _max_row("A", buttons=VALID_BUTTONS)
    poison = _max_row("B", buttons=MALFORMED_BUTTONS)
    last = _max_row("C", buttons=VALID_BUTTONS)

    with caplog.at_level(logging.ERROR, logger="apps.customer_requests.max_bot"):
        max_worker.run(once=True)  # must not raise

    assert _status(first) == MaxDeliveryStatus.SENT
    assert _status(last) == MaxDeliveryStatus.SENT
    assert _status(poison) == MaxDeliveryStatus.FAILED
    assert "KeyError" in poison.last_error
    assert poison.attempts == 1
    assert server.texts_to(CHAT) == ["A", "C"]
    assert "KeyError" in MaxBotRuntime.objects.get().last_error
    assert "KeyError at max_api.py" in caplog.text and "inline_keyboard" in caplog.text

    # The failed row stays failed: later cycles never send it.
    max_worker.acquire()
    for _ in range(3):
        max_worker.iterate()
    assert server.texts_to(CHAT) == ["A", "C"]
    assert _status(poison) == MaxDeliveryStatus.FAILED


def test_max_failure_after_the_send_started_is_uncertain_and_never_resent(max_worker, server):
    rows = [_max_row(text) for text in ("A", "B", "C")]
    real_send = max_worker.api.send_message

    def send_then_break(**kwargs):
        result = real_send(**kwargs)
        if kwargs["text"] == "B":
            raise RuntimeError("client defect after MAX accepted the message")
        return result

    max_worker.api.send_message = send_then_break
    max_worker.run(once=True)

    assert [_status(row) for row in rows] == [
        MaxDeliveryStatus.SENT, MaxDeliveryStatus.UNCERTAIN, MaxDeliveryStatus.SENT,
    ]
    assert "RuntimeError" in rows[1].last_error
    max_worker.acquire()
    for _ in range(3):
        max_worker.iterate()
    assert server.texts_to(CHAT) == ["A", "B", "C"]  # B reached MAX once, never again


def test_max_failure_after_the_row_is_sent_keeps_it_sent(max_worker, server, monkeypatch):
    rows = [_max_row(text) for text in ("A", "B", "C")]
    real_confirm = max_bot.operator_replies.confirm_responder_transition

    def confirm(row):
        if row.text == "B":
            raise ValueError("defect after the send was recorded")
        return real_confirm(row)

    monkeypatch.setattr(max_bot.operator_replies, "confirm_responder_transition", confirm)
    max_worker.run(once=True)

    assert [_status(row) for row in rows] == [MaxDeliveryStatus.SENT] * 3
    assert server.texts_to(CHAT) == ["A", "B", "C"]


def test_max_transient_refusal_and_poison_row_do_not_stop_another_dialog(max_worker, server):
    server.script("/messages", ("status", 503, {"code": "x", "message": "busy"}))
    waiting = _max_row("A")
    poison = _max_row("B", buttons=MALFORMED_BUTTONS)
    other = _max_row("C", chat=OTHER_CHAT)

    max_worker.run(once=True)

    waiting.refresh_from_db()
    assert waiting.delivery_status == MaxDeliveryStatus.PENDING  # retried later, bounded
    assert waiting.next_attempt_at > timezone.now()
    # B waits behind A in its dialog (order is kept); it is not lost.
    assert _status(poison) == MaxDeliveryStatus.PENDING
    assert _status(other) == MaxDeliveryStatus.SENT
    assert server.texts_to(OTHER_CHAT) == ["C"]

    MaxMessage.objects.filter(pk__in=[waiting.pk, poison.pk]).update(
        next_attempt_at=timezone.now()
    )
    max_worker.acquire()
    max_worker.iterate()
    assert _status(waiting) == MaxDeliveryStatus.SENT
    assert _status(poison) == MaxDeliveryStatus.FAILED
    assert server.texts_to(CHAT) == ["A"]


def _finish_failing_on(status):
    original = MaxBotWorker._finish

    def finish(self, row, row_status, **kwargs):
        if row_status == status:
            raise OperationalError("database went away")
        return original(self, row, row_status, **kwargs)

    return finish


def test_max_database_fault_while_marking_sent_never_resends_or_fakes_success(
    max_worker, server, monkeypatch
):
    rows = [_max_row(text) for text in ("A", "B")]
    original = MaxBotWorker._finish
    monkeypatch.setattr(MaxBotWorker, "_finish", _finish_failing_on(MaxDeliveryStatus.SENT))
    max_worker.run(once=True)  # the database path, not a crash
    assert _status(rows[0]) == MaxDeliveryStatus.SENDING
    monkeypatch.setattr(MaxBotWorker, "_finish", original)

    restarted = MaxBotWorker(max_worker.api, worker_id="max-rel",
                             heartbeat_file="", pacer=SendPacer(sleep=lambda s: None))
    restarted.start()
    restarted.iterate()
    assert [_status(row) for row in rows] == [MaxDeliveryStatus.UNCERTAIN] * 2
    assert server.texts_to(CHAT) == ["A"]  # sent once, never again, B never sent
    assert not MaxMessage.objects.filter(delivery_status=MaxDeliveryStatus.SENT).exists()


def test_max_database_fault_while_marking_failed_is_uncertain_not_sent(
    max_worker, server, monkeypatch
):
    poison = _max_row("B", buttons=MALFORMED_BUTTONS)
    original = MaxBotWorker._finish
    monkeypatch.setattr(MaxBotWorker, "_finish", _finish_failing_on(MaxDeliveryStatus.FAILED))
    max_worker.run(once=True)
    monkeypatch.setattr(MaxBotWorker, "_finish", original)

    max_worker.start()
    max_worker.iterate()
    assert _status(poison) == MaxDeliveryStatus.UNCERTAIN
    assert server.texts_to(CHAT) == []


def test_max_cycle_defect_keeps_the_worker_alive_and_visible(max_worker, server, caplog):
    row = _max_row("A")
    with mock.patch.object(
        max_bot.service, "announce_new_requests", side_effect=RuntimeError(SECRET_TEXT)
    ), caplog.at_level(logging.ERROR, logger="apps.customer_requests.max_bot"):
        max_worker.run(once=True)  # must not raise
    assert MaxBotRuntime.objects.get().last_error == "Цикл: RuntimeError"
    assert "RuntimeError at" in caplog.text and "iterate" in caplog.text
    assert SECRET_TEXT not in caplog.text
    assert _status(row) == MaxDeliveryStatus.PENDING  # untouched by the failed cycle

    max_worker.run(once=True)
    assert _status(row) == MaxDeliveryStatus.SENT


def test_max_worker_loop_goes_on_after_an_unexpected_cycle_failure(max_worker):
    calls = []

    def cycle():
        calls.append(1)
        if len(calls) == 1:
            raise TypeError("defect")
        max_worker.stop.set()

    with mock.patch.object(max_worker, "iterate", side_effect=cycle):
        max_worker.run()
    assert len(calls) == 2


def test_max_worker_outlives_a_database_outage_seen_by_its_idle_check(max_worker, monkeypatch):
    checks = []

    def has_due_work():
        checks.append(1)
        if len(checks) == 1:
            raise OperationalError("database is down")
        max_worker.stop.set()
        return False

    monkeypatch.setattr(max_worker, "has_due_work", has_due_work)
    max_worker.run()  # must not raise
    assert len(checks) == 2
    assert max_worker._needs_recovery is False  # the second cycle recovered


def test_max_console_notification_poison_is_retried_alone(max_worker, server, monkeypatch):
    rows = [SimpleNamespace(pk=pk) for pk in (1, 2, 3)]
    finished, retried = [], []
    monkeypatch.setattr(operator_console, "enabled", lambda: True)
    monkeypatch.setattr(operator_console, "claim_notifications", lambda provider, limit: rows)
    monkeypatch.setattr(
        operator_console,
        "prepare_notification_delivery",
        lambda notification_id, provider: SimpleNamespace(
            delivery_chat_id=CHAT, text=f"N{notification_id}", buttons=notification_id
        ),
    )
    monkeypatch.setattr(
        operator_console,
        "buttons_for_provider",
        lambda buttons, provider: {
            "inline_keyboard": MALFORMED_BUTTONS if buttons == 2 else VALID_BUTTONS
        },
    )
    monkeypatch.setattr(
        operator_console, "finish_notification",
        lambda row, *, status, external_id="", error="": finished.append((row.pk, status)),
    )
    monkeypatch.setattr(
        operator_console, "retry_notification",
        lambda row, error: retried.append((row.pk, str(error))),
    )
    max_worker.start()
    assert max_worker.send_operator_console_notifications() == 3

    sent = operator_console.OperatorNotification.Status.SENT
    assert finished == [(1, sent), (3, sent)]
    assert retried == [(2, "KeyError")]
    assert server.texts_to(CHAT) == ["N1", "N3"]


def test_max_console_notification_failing_after_the_send_is_uncertain(
    max_worker, server, monkeypatch
):
    finished = []
    monkeypatch.setattr(operator_console, "enabled", lambda: True)
    monkeypatch.setattr(
        operator_console, "claim_notifications", lambda provider, limit: [SimpleNamespace(pk=7)]
    )
    monkeypatch.setattr(
        operator_console, "prepare_notification_delivery",
        lambda notification_id, provider: SimpleNamespace(
            delivery_chat_id=CHAT, text="N7", buttons=None
        ),
    )
    monkeypatch.setattr(operator_console, "buttons_for_provider", lambda buttons, provider: None)
    monkeypatch.setattr(
        operator_console, "finish_notification",
        lambda row, *, status, external_id="", error="": finished.append(status),
    )
    max_worker.start()
    with mock.patch.object(max_worker.api, "send_message", side_effect=AttributeError("x")):
        max_worker.send_operator_console_notifications()
    assert finished == [operator_console.OperatorNotification.Status.UNCERTAIN]


def test_max_webhook_defect_is_logged_with_frames_and_without_the_payload(client, db, caplog):
    with mock.patch.object(
        max_bot, "handle_update", side_effect=RuntimeError(SECRET_TEXT)
    ), caplog.at_level(logging.ERROR):
        response = deliver(client, message_created(41_000_009, CHAT, SECRET_TEXT))
    assert response.status_code == 200  # rolled back and acknowledged: no redelivery loop
    assert not MaxMessage.objects.exists()
    assert "RuntimeError at" in caplog.text and "max_webhook" in caplog.text
    assert SECRET_TEXT not in caplog.text


def test_max_webhook_database_fault_is_visible_and_asks_for_redelivery(client, db, caplog):
    with mock.patch.object(
        max_bot, "handle_update", side_effect=OperationalError("down")
    ), caplog.at_level(logging.ERROR):
        response = deliver(client, message_created(41_000_009, CHAT, "x"))
    assert response.status_code == 503
    assert "max webhook storage failed: OperationalError" in caplog.text


# --- Malformed HTTP answers (both clients) ----------------------------------------------


@pytest.fixture
def raw_http():
    """A socket that answers each request with fixed bytes: what no client must crash on."""
    replies = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(5)

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                conn.sendall(replies.pop(0) if replies else b"")

    threading.Thread(target=serve, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{listener.getsockname()[1]}", replies=replies)
    listener.close()


GARBLED_STATUS = b"GARBAGE\r\n\r\n"
CUT_SHORT = b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"ok\":"
LIST_BODY = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 3\r\n\r\n[1]"
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


@pytest.mark.parametrize("reply", [GARBLED_STATUS, CUT_SHORT], ids=["garbled", "cut-short"])
def test_max_client_turns_a_broken_http_answer_into_a_network_error(raw_http, reply):
    api = MaxBotApi(FAKE_MAX_TOKEN, base_url=raw_http.url, timeout=2, opener=DIRECT)
    raw_http.replies.extend([reply, reply])
    with pytest.raises(MaxNetworkError) as sent:
        api.send_message(chat_id=CHAT, text="x")
    assert sent.value.ambiguous  # a send may have been read: uncertain, never resent
    with pytest.raises(MaxNetworkError) as read:
        api.get_me()
    assert not read.value.ambiguous


@pytest.mark.parametrize("reply", [GARBLED_STATUS, CUT_SHORT], ids=["garbled", "cut-short"])
def test_telegram_client_turns_a_broken_http_answer_into_a_network_error(raw_http, reply):
    api = TelegramBotApi(TG_FAKE_TOKEN, base_url=raw_http.url, timeout=2, opener=DIRECT)
    raw_http.replies.extend([reply, reply])
    with pytest.raises(TelegramNetworkError) as sent:
        api.send_message(chat_id=1, text="x")
    assert sent.value.ambiguous
    with pytest.raises(TelegramNetworkError) as read:
        api.get_updates(offset=1, timeout=0)
    assert not read.value.ambiguous
    assert TG_FAKE_TOKEN not in str(sent.value) + str(read.value)


def test_telegram_file_send_with_an_unusable_answer_is_uncertain(raw_http):
    api = TelegramBotApi(TG_FAKE_TOKEN, base_url=raw_http.url, timeout=2, opener=DIRECT)
    raw_http.replies.append(LIST_BODY)
    with pytest.raises(TelegramNetworkError) as error:
        api.send_file(chat_id=1, content=b"x", filename="a.pdf", content_type="application/pdf")
    assert error.value.ambiguous


# --- Telegram -----------------------------------------------------------------------------


@pytest.fixture
def api():
    return FakeBotApi()


@pytest.fixture
def tg_worker(db, api, no_pause):
    worker = TelegramBotWorker(api, worker_id="tg-rel", poll_timeout=0, heartbeat_file="")
    worker.stop.wait = lambda seconds=None: False
    worker.start()
    return worker


STRANGERS = (930_001, 930_002, 930_003)


def _updates():
    return [message_update(user, "привет") for user in STRANGERS]


def _failing_on(update_id, exc, *, times=None):
    real = telegram_bot.handle_update
    calls = []

    def handle(update, **kwargs):
        if update["update_id"] == update_id and (times is None or len(calls) < times):
            calls.append(update_id)
            raise exc
        return real(update, **kwargs)

    handle.calls = calls
    return handle


def test_telegram_poison_update_is_skipped_alone_and_logged_without_payload(
    tg_worker, api, monkeypatch, caplog
):
    updates = _updates()
    poison = updates[1]["update_id"]
    monkeypatch.setattr(telegram_bot, "handle_update", _failing_on(poison, KeyError(SECRET_TEXT)))
    api.updates.extend(updates)
    with caplog.at_level(logging.ERROR, logger="apps.customer_requests.telegram_bot"):
        tg_worker.run(once=True)

    assert TelegramBotRuntime.objects.get().last_update_id == updates[2]["update_id"]
    answered = {item["chat_id"] for item in api.sent}
    assert answered == {STRANGERS[0], STRANGERS[2]}
    assert f"update {poison} failed: KeyError at" in caplog.text
    assert SECRET_TEXT not in caplog.text


def test_telegram_database_fault_on_an_update_retries_it_instead_of_skipping(
    tg_worker, api, monkeypatch
):
    updates = _updates()
    flaky = updates[1]["update_id"]
    monkeypatch.setattr(
        telegram_bot, "handle_update", _failing_on(flaky, OperationalError("deadlock"), times=1)
    )
    api.updates.extend(updates)

    tg_worker.run(once=True)
    assert TelegramBotRuntime.objects.get().last_update_id == updates[0]["update_id"]
    tg_worker.run(once=True)

    assert TelegramBotRuntime.objects.get().last_update_id == updates[2]["update_id"]
    assert {item["chat_id"] for item in api.sent} == set(STRANGERS)  # nobody was dropped


def test_telegram_update_failing_on_the_database_every_time_is_skipped_after_bounded_retries(
    tg_worker, api, monkeypatch
):
    updates = _updates()
    stuck = updates[1]["update_id"]
    handler = _failing_on(stuck, OperationalError("always"))
    monkeypatch.setattr(telegram_bot, "handle_update", handler)
    api.updates.extend(updates)

    for _ in range(telegram_bot.UPDATE_DATABASE_ATTEMPTS):
        tg_worker.run(once=True)

    assert handler.calls == [stuck] * telegram_bot.UPDATE_DATABASE_ATTEMPTS
    assert TelegramBotRuntime.objects.get().last_update_id == updates[2]["update_id"]
    assert {item["chat_id"] for item in api.sent} == {STRANGERS[0], STRANGERS[2]}
    assert "OperationalError" in TelegramBotRuntime.objects.get().last_error


def test_telegram_failing_reply_does_not_stop_the_worker_or_lose_the_next_update(
    tg_worker, api, monkeypatch
):
    updates = _updates()
    real_send = api.send_message

    def send(**kwargs):
        if kwargs["chat_id"] == STRANGERS[0]:
            raise TypeError("defect while building the reply")
        return real_send(**kwargs)

    monkeypatch.setattr(api, "send_message", send)
    api.updates.extend(updates)
    tg_worker.run(once=True)  # must not raise
    assert "TypeError" in TelegramBotRuntime.objects.get().last_error
    tg_worker.run(once=True)

    assert TelegramBotRuntime.objects.get().last_update_id == updates[2]["update_id"]
    assert {item["chat_id"] for item in api.sent} == {STRANGERS[1], STRANGERS[2]}


def test_telegram_cycle_defect_keeps_the_worker_alive(tg_worker, api):
    api.get_updates_error = ValueError(SECRET_TEXT)
    tg_worker.run(once=True)  # must not raise
    assert TelegramBotRuntime.objects.get().last_error == "Цикл: ValueError"

    api.get_updates_error = None
    api.updates.extend(_updates()[:1])
    tg_worker.run(once=True)
    assert [item["chat_id"] for item in api.sent] == [STRANGERS[0]]


def test_telegram_worker_loop_goes_on_after_an_unexpected_cycle_failure(tg_worker):
    calls = []

    def cycle():
        calls.append(1)
        if len(calls) == 1:
            raise AttributeError("defect")
        tg_worker.stop.set()

    with mock.patch.object(tg_worker, "iterate", side_effect=cycle):
        tg_worker.run()
    assert len(calls) == 2


def test_telegram_operator_card_poison_fails_alone(
    tg_worker, api, monkeypatch, django_user_model, caplog
):
    _operator(django_user_model, OPERATOR_A, username="denis")
    _operator(django_user_model, OPERATOR_B, username="masha")
    part = build_part()
    requests = [_request(part, key=str(n) * 32) for n in (1, 2, 3)]
    real_content = telegram_service_module.delivery_content

    def content(event):
        if event.request_id == requests[1].pk:
            raise KeyError("broken card")
        return real_content(event)

    monkeypatch.setattr(telegram_service_module, "delivery_content", content)
    with caplog.at_level(logging.ERROR, logger="apps.customer_requests.telegram_bot"):
        tg_worker.run(once=True)

    statuses = {
        (row.event.request_id, row.status) for row in TelegramDelivery.objects.select_related(
            "event"
        )
    }
    assert statuses == {
        (requests[0].pk, TelegramDeliveryStatus.SENT),
        (requests[1].pk, TelegramDeliveryStatus.FAILED),
        (requests[2].pk, TelegramDeliveryStatus.SENT),
    }
    assert TelegramDelivery.objects.filter(status=TelegramDeliveryStatus.FAILED).count() == 2
    assert len(api.sent) == 4
    assert "KeyError at" in caplog.text


def test_telegram_customer_row_failing_mid_send_is_uncertain_and_neighbours_are_sent(
    tg_worker, api, monkeypatch
):
    part = build_part()
    first, second = (_request(part, key=str(n) * 32) for n in (4, 5))
    link(tg_worker, api, first, chat_id=STRANGERS[0])
    link(tg_worker, api, second, chat_id=STRANGERS[1])
    conversations = {c.request_id: c for c in TelegramConversation.objects.all()}

    def reply(request, text):
        return TelegramMessage.objects.create(
            conversation=conversations[request.pk],
            direction=TelegramMessage.Direction.OPERATOR,
            text=text,
            delivery_status=TelegramDeliveryStatus.PENDING,
            next_attempt_at=timezone.now(),
        )

    rows = [reply(first, "ответ A"), reply(second, "ответ B"), reply(first, "ответ C")]
    real_send = api.send_message

    def send(**kwargs):
        if kwargs["text"] == "ответ B":
            raise TypeError("defect inside the client")
        return real_send(**kwargs)

    monkeypatch.setattr(api, "send_message", send)
    tg_worker.run(once=True)
    for _ in range(2):
        tg_worker.run(once=True)

    assert [_status_tg(row) for row in rows] == [
        TelegramDeliveryStatus.SENT, TelegramDeliveryStatus.UNCERTAIN, TelegramDeliveryStatus.SENT,
    ]
    texts = [item["text"] for item in api.sent]
    assert texts.count("ответ A") == texts.count("ответ C") == 1
    assert "ответ B" not in texts


def test_telegram_customer_row_failing_before_the_send_is_failed(tg_worker, api, monkeypatch):
    part = build_part()
    request = _request(part, key="6" * 32)
    link(tg_worker, api, request, chat_id=STRANGERS[0])
    row = TelegramMessage.objects.create(
        conversation=TelegramConversation.objects.get(request=request),
        direction=TelegramMessage.Direction.OPERATOR,
        text="ответ",
        delivery_status=TelegramDeliveryStatus.PENDING,
        next_attempt_at=timezone.now(),
    )
    monkeypatch.setattr(
        telegram_bot.service, "customer_keyboard", mock.Mock(side_effect=ValueError("defect"))
    )
    tg_worker.run(once=True)
    assert _status_tg(row) == TelegramDeliveryStatus.FAILED
    assert "ValueError" in row.last_error


def _status_tg(row):
    row.refresh_from_db()
    return row.delivery_status


# --- Observability -----------------------------------------------------------------------


def test_exception_trace_names_class_and_frames_but_never_the_message():
    def inner():
        raise ValueError(f"token=abc {SECRET_TEXT}")

    try:
        inner()
    except ValueError as exc:
        trace = exception_trace(exc)
    assert trace.startswith("ValueError at test_bot_worker_reliability.py:")
    assert "inner" in trace
    assert SECRET_TEXT not in trace and "abc" not in trace
