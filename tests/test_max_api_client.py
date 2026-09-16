"""The MAX Bot API client against a real local HTTP server.

Every failure a sender must tell apart is produced by ``FakeMaxServer`` over a
real socket: refusals, rate limits, outages, a dead port, a stalled answer,
garbage bodies, and the dangerous one - a send MAX accepted whose answer never
arrived. No network beyond 127.0.0.1 and no real token.
"""

import logging
import socket

import pytest

from apps.customer_requests.max_api import (
    MaxApiError,
    MaxBotApi,
    MaxNetworkError,
    inline_keyboard,
    webhook_secret_is_well_formed,
)

from .max_fake import FAKE_BOT_USERNAME, FAKE_MAX_TOKEN, FakeMaxServer


@pytest.fixture
def server():
    fake = FakeMaxServer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def api(server):
    return MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=2)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# 1, 2 ----------------------------------------------------------------------------------


def test_token_travels_only_in_the_authorization_header(server, api):
    api.get_me()
    api.send_message(chat_id=5001, text="Привет")

    assert {item["authorization"] for item in server.requests} == {FAKE_MAX_TOKEN}
    for item in server.requests:
        assert FAKE_MAX_TOKEN not in item["url"]
    assert "access_token" not in server.all_requests_text()


def test_the_client_never_renders_its_token():
    client = MaxBotApi(FAKE_MAX_TOKEN, base_url="http://127.0.0.1:1")
    assert FAKE_MAX_TOKEN not in repr(client)
    with pytest.raises(ValueError):
        MaxBotApi("")


# 3, 4 ----------------------------------------------------------------------------------


def test_get_me_returns_the_bot_identity(api):
    me = api.get_me()
    assert me["username"] == FAKE_BOT_USERNAME
    assert isinstance(me["user_id"], int)


def test_send_returns_the_created_message_with_its_mid(server, api):
    message = api.send_message(
        chat_id=5001, text="Выберите заявку", buttons=[[{"text": "Заявка A", "payload": "s:ab"}]]
    )

    assert message["body"]["mid"].startswith("mid.")
    request = server.requests[-1]
    assert request["method"] == "POST" and request["path"] == "/messages"
    assert request["query"]["chat_id"] == "5001"
    assert request["body"]["attachments"] == inline_keyboard([[{"text": "Заявка A",
                                                                "payload": "s:ab"}]])
    assert server.sent[-1]["text"] == "Выберите заявку"


# 5, 6, 7, 8 ------------------------------------------------------------------------------


def test_401_is_a_final_refusal(server):
    wrong = MaxBotApi("fake-wrong-token-for-tests", base_url=server.base_url, timeout=2)
    with pytest.raises(MaxApiError) as caught:
        wrong.send_message(chat_id=5001, text="x")
    assert caught.value.status == 401
    assert caught.value.code == "verify.token"
    assert caught.value.retryable is False
    assert server.sent == []


def test_validation_4xx_is_a_final_refusal(server, api):
    server.script("/messages", ("status", 400, {"code": "proto.payload", "message": "bad"}))
    with pytest.raises(MaxApiError) as caught:
        api.send_message(chat_id=5001, text="x")
    assert (caught.value.status, caught.value.retryable) == (400, False)


def test_429_is_retryable_and_carries_retry_after(server, api):
    server.script(
        "/messages",
        ("status", 429, {"code": "too.many.requests", "message": "slow down"},
         {"Retry-After": "7"}),
    )
    with pytest.raises(MaxApiError) as caught:
        api.send_message(chat_id=5001, text="x")
    assert caught.value.retryable is True
    assert caught.value.retry_after == 7
    assert server.sent == []


def test_absurd_retry_after_is_ignored(server, api):
    server.script(
        "/messages",
        ("status", 429, {"code": "too.many.requests", "message": "x"},
         {"Retry-After": "999999"}),
    )
    with pytest.raises(MaxApiError) as caught:
        api.send_message(chat_id=5001, text="x")
    assert caught.value.retry_after is None


@pytest.mark.parametrize("status", [500, 503])
def test_5xx_is_retryable(server, api, status):
    server.script("/messages", ("status", status, {"code": "internal", "message": "oops"}))
    with pytest.raises(MaxApiError) as caught:
        api.send_message(chat_id=5001, text="x")
    assert caught.value.retryable is True


@pytest.mark.parametrize("status", [502, 504])
def test_a_gateway_timeout_on_send_is_ambiguous(server, api, status):
    server.script("/messages", ("status", status, {"code": "gateway", "message": "x"}))
    with pytest.raises(MaxNetworkError) as caught:
        api.send_message(chat_id=5001, text="x")
    assert caught.value.ambiguous is True


# 9, 10, 11 -----------------------------------------------------------------------------


def test_connection_refused_is_certainly_not_sent():
    client = MaxBotApi(FAKE_MAX_TOKEN, base_url=f"http://127.0.0.1:{_closed_port()}", timeout=1)
    with pytest.raises(MaxNetworkError) as caught:
        client.send_message(chat_id=5001, text="x")
    assert caught.value.ambiguous is False


def test_a_timeout_of_a_read_call_is_safe_to_repeat(server, api):
    server.script("/me", ("delay", 1.5))
    slow = MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=0.3)
    with pytest.raises(MaxNetworkError) as caught:
        slow.get_me()
    assert caught.value.ambiguous is False


def test_a_send_accepted_but_never_answered_is_ambiguous(server):
    server.script("/messages", ("accept_then_hang", 1.5))
    slow = MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=0.3)
    with pytest.raises(MaxNetworkError) as caught:
        slow.send_message(chat_id=5001, text="Ответ менеджера")
    assert caught.value.ambiguous is True
    # MAX really has it: resending would duplicate the customer's message.
    assert server.texts_to(5001) == ["Ответ менеджера"]


def test_a_dropped_connection_during_a_send_is_ambiguous(server, api):
    server.script("/messages", ("drop",))
    with pytest.raises(MaxNetworkError) as caught:
        api.send_message(chat_id=5001, text="x")
    assert caught.value.ambiguous is True


# 12 ------------------------------------------------------------------------------------


def test_malformed_json_on_a_send_is_ambiguous_and_on_a_read_is_not(server, api):
    server.script("/messages", ("malformed",))
    with pytest.raises(MaxNetworkError) as caught:
        api.send_message(chat_id=5001, text="x")
    assert caught.value.ambiguous is True

    server.script("/me", ("malformed",))
    with pytest.raises(MaxNetworkError) as caught:
        api.get_me()
    assert caught.value.ambiguous is False


def test_success_with_an_unusable_body_is_ambiguous_for_a_send(server, api):
    server.script("/messages", ("empty_success",))
    with pytest.raises(MaxNetworkError) as caught:
        api.send_message(chat_id=5001, text="x")
    assert caught.value.ambiguous is True


def test_oversized_text_is_refused_locally_without_a_call(server, api):
    with pytest.raises(MaxApiError):
        api.send_message(chat_id=5001, text="я" * 4001)
    assert server.requests == []


# Subscriptions and callbacks --------------------------------------------------------------


def test_subscription_lifecycle_and_callback_answer(server, api):
    url = "https://prostor.example/customer-requests/max/webhook/"
    api.subscribe(url=url, secret="fake_secret-1", update_types=["message_created"])
    assert [item["url"] for item in api.list_subscriptions()] == [url]
    assert server.requests[-2]["body"]["secret"] == "fake_secret-1"

    api.unsubscribe(url=url)
    assert api.list_subscriptions() == []

    api.answer_callback(callback_id="cb.1", notification="Готово")
    assert server.answers == [{"callback_id": "cb.1", "body": {"notification": "Готово"}}]


def test_subscription_refuses_http_and_malformed_secrets(api):
    with pytest.raises(ValueError):
        api.subscribe(url="http://insecure.example/hook", secret="abcdef", update_types=[])
    with pytest.raises(ValueError):
        api.subscribe(url="https://x.example/hook", secret="bad secret!", update_types=[])
    assert webhook_secret_is_well_formed("a" * 5)
    assert not webhook_secret_is_well_formed("a" * 4)
    assert not webhook_secret_is_well_formed("a" * 257)


def test_errors_never_carry_the_token(server, caplog):
    echoing = FakeMaxServer(token=FAKE_MAX_TOKEN)
    echoing.start()
    try:
        echoing.script(
            "/messages",
            ("status", 400, {"code": "x", "message": f"bad header {FAKE_MAX_TOKEN}"}),
        )
        client = MaxBotApi(FAKE_MAX_TOKEN, base_url=echoing.base_url, timeout=2)
        with caplog.at_level(logging.DEBUG), pytest.raises(MaxApiError) as caught:
            client.send_message(chat_id=1, text="x")
    finally:
        echoing.stop()
    assert FAKE_MAX_TOKEN not in str(caught.value)
    assert "<redacted>" in str(caught.value)
    assert FAKE_MAX_TOKEN not in caplog.text
