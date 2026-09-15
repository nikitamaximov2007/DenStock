"""Telegram worker startup: an outage waits in-process, refusals stay refusals.

Found in production: an unreachable Telegram at startup killed the process and
the container restart policy turned that into a loop. These tests pin the fix
and the unchanged refusal semantics.
"""

import logging
import threading
import time
import urllib.error
import urllib.request

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

import apps.customer_requests.management.commands.run_telegram_bot as command_module
from apps.customer_requests import telegram_bot
from apps.customer_requests.telegram_api import (
    TelegramApiError,
    TelegramBotApi,
    TelegramNetworkError,
)
from apps.customer_requests.telegram_bot import (
    STARTUP_BACKOFF_MAX_SECONDS,
    SingleInstanceError,
    TelegramBotWorker,
    startup_backoff_seconds,
)
from apps.operations.models import TelegramBotRuntime
from tests.test_telegram_customer_messaging import FAKE_TOKEN, FakeBotApi, _Response

PROXY = "http://10.231.0.1:2081"


class FlakyStartApi(FakeBotApi):
    def __init__(self, failures):
        super().__init__()
        self.startup_failures = list(failures)
        self.webhook_calls = 0

    def get_webhook_info(self):
        self.webhook_calls += 1
        if self.startup_failures:
            raise self.startup_failures.pop(0)
        return self.webhook


class RecordingStop(threading.Event):
    """Waits return at once and are recorded; optionally stops after N waits."""

    def __init__(self, stop_after=None):
        super().__init__()
        self.waits = []
        self.stop_after = stop_after

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.stop_after is not None and len(self.waits) >= self.stop_after:
            self.set()
        return self.is_set()


def _outage(count):
    return [TelegramNetworkError("OSError", ambiguous=False) for _ in range(count)]


def test_startup_backoff_is_exponential_and_bounded():
    delays = [startup_backoff_seconds(attempt) for attempt in range(1, 12)]
    assert delays[:4] == [5, 10, 20, 40]
    assert delays == sorted(delays)
    assert max(delays) == STARTUP_BACKOFF_MAX_SECONDS == 300


def test_network_outage_at_startup_retries_in_process_then_starts(db, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    api = FlakyStartApi(_outage(2))
    stop = RecordingStop()
    worker = TelegramBotWorker(
        api, stop=stop, worker_id="w", poll_timeout=0, heartbeat_file=str(heartbeat)
    )

    assert worker.start_with_retry() is True
    assert api.webhook_calls == 3
    assert stop.waits == [5, 10]
    runtime = TelegramBotRuntime.objects.get()
    assert runtime.worker_id == "w"
    assert runtime.bot_username == "ProStorTestBot"
    assert runtime.last_error.startswith("Старт:")
    assert heartbeat.exists()


def test_repeated_outage_never_exits_and_never_looks_healthy(db, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    api = FlakyStartApi(_outage(50))
    stop = RecordingStop(stop_after=6)
    worker = TelegramBotWorker(
        api, stop=stop, worker_id="w", poll_timeout=0, heartbeat_file=str(heartbeat)
    )

    worker.run()  # returns only because a stop was requested

    assert api.webhook_calls == 5
    assert stop.waits == [5, 10, 20, 30, 10, 30]  # lease renewed between 30 s slices
    assert not heartbeat.exists()
    assert TelegramBotRuntime.objects.get().worker_id == ""


def test_stop_during_startup_backoff_ends_promptly_and_releases_the_lease(db, monkeypatch):
    monkeypatch.setattr(telegram_bot, "startup_backoff_seconds", lambda attempt: 300)
    api = FlakyStartApi(_outage(10))
    stop = threading.Event()
    worker = TelegramBotWorker(api, stop=stop, worker_id="sig", poll_timeout=0, heartbeat_file="")
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    started = time.monotonic()

    worker.run()

    timer.cancel()
    assert time.monotonic() - started < 5
    assert api.webhook_calls == 1
    assert TelegramBotRuntime.objects.get().worker_id == ""


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_refused_token_or_request_at_startup_is_a_hard_refusal(db, code):
    api = FlakyStartApi([TelegramApiError(code, "Unauthorized")])
    stop = RecordingStop()
    worker = TelegramBotWorker(api, stop=stop, worker_id="w", poll_timeout=0, heartbeat_file="")

    with pytest.raises(SingleInstanceError, match=str(code)):
        worker.run()

    assert stop.waits == []
    assert api.webhook_calls == 1
    assert TelegramBotRuntime.objects.get().worker_id == ""


def test_rate_limit_and_server_errors_at_startup_are_retried(db):
    api = FlakyStartApi(
        [
            TelegramApiError(429, "Too Many Requests", retry_after=42),
            TelegramApiError(502, "Bad Gateway"),
        ]
    )
    stop = RecordingStop()
    worker = TelegramBotWorker(api, stop=stop, worker_id="w", poll_timeout=0, heartbeat_file="")

    assert worker.start_with_retry() is True
    assert stop.waits == [30, 12, 10]  # retry_after honoured, in lease slices


def test_configured_webhook_is_a_deliberate_refusal_not_an_outage(db):
    api = FlakyStartApi([])
    api.webhook = {"url": "https://example.invalid/hook"}
    stop = RecordingStop()

    with pytest.raises(SingleInstanceError, match="webhook"):
        TelegramBotWorker(api, stop=stop, worker_id="w", heartbeat_file="").run()

    assert stop.waits == []


def test_conflict_while_polling_is_a_refusal(db):
    api = FakeBotApi()
    api.get_updates_error = TelegramApiError(409, "Conflict: terminated by other getUpdates")

    with pytest.raises(SingleInstanceError, match="409"):
        TelegramBotWorker(
            api, stop=RecordingStop(), worker_id="w", poll_timeout=0, heartbeat_file=""
        ).run()

    assert TelegramBotRuntime.objects.get().worker_id == ""


def test_malformed_startup_answers_are_outages_not_crashes():
    api = TelegramBotApi(
        FAKE_TOKEN, opener=lambda request, timeout: _Response(b'{"ok":true,"result":[1]}')
    )
    with pytest.raises(TelegramNetworkError):
        api.get_webhook_info()
    with pytest.raises(TelegramNetworkError):
        api.get_me()


def test_malformed_startup_answer_is_retried(db):
    api = FlakyStartApi([TelegramNetworkError("invalid response", ambiguous=False)])
    stop = RecordingStop()
    worker = TelegramBotWorker(api, stop=stop, worker_id="w", poll_timeout=0, heartbeat_file="")
    assert worker.start_with_retry() is True
    assert stop.waits == [5]


def test_heartbeat_follows_telegram_polls_not_only_the_database(db, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    api = FakeBotApi()
    worker = TelegramBotWorker(
        api, stop=RecordingStop(), worker_id="w", poll_timeout=0, heartbeat_file=str(heartbeat)
    )
    worker.start()
    assert heartbeat.exists()
    heartbeat.unlink()

    api.get_updates_error = TelegramNetworkError("OSError", ambiguous=False)
    with pytest.raises(TelegramNetworkError):
        worker.iterate(poll_timeout=0)
    assert not heartbeat.exists()

    api.get_updates_error = None
    worker.iterate(poll_timeout=0)
    assert heartbeat.exists()


def test_bot_api_uses_no_proxy_unless_explicitly_configured():
    direct = TelegramBotApi(FAKE_TOKEN)
    assert direct._opener is urllib.request.urlopen
    assert "proxy=False" in repr(direct)

    proxied = TelegramBotApi(FAKE_TOKEN, proxy_url=PROXY)
    handler = next(
        item
        for item in proxied._opener.__self__.handlers
        if isinstance(item, urllib.request.ProxyHandler)
    )
    assert handler.proxies == {"https": PROXY, "http": PROXY}
    assert "proxy=True" in repr(proxied)
    assert FAKE_TOKEN not in repr(proxied)


@pytest.mark.parametrize(
    "value",
    [
        "https://10.231.0.1:2081",
        "http://user:hunter2@10.231.0.1:2081",
        "http://10.231.0.1",
        "socks5://10.231.0.1:2081",
        "http://10.231.0.1:2081/path",
        "http://:2081",
        "http://10.231.0.1:notaport",
    ],
)
def test_invalid_proxy_url_is_refused_without_echoing_it(value):
    with pytest.raises(ValueError) as error:
        TelegramBotApi(FAKE_TOKEN, proxy_url=value)
    assert value not in str(error.value)
    assert "hunter2" not in str(error.value)


def test_proxy_tunnel_refusal_never_leaks_the_token(db, caplog):
    def refusing(request, timeout):
        raise urllib.error.URLError(OSError(f"Tunnel connection failed: 403 {FAKE_TOKEN}"))

    api = TelegramBotApi(FAKE_TOKEN, opener=refusing)
    worker = TelegramBotWorker(
        api, stop=RecordingStop(stop_after=1), worker_id="w", poll_timeout=0, heartbeat_file=""
    )
    logger = logging.getLogger("apps.customer_requests.telegram_bot")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            worker.run()
    finally:
        logger.removeHandler(caplog.handler)

    runtime = TelegramBotRuntime.objects.get()
    assert runtime.last_error.startswith("Старт:")
    assert FAKE_TOKEN not in caplog.text + runtime.last_error
    assert "telegram unavailable at startup" in caplog.text


def _command_setup(settings, monkeypatch, *, proxy=""):
    settings.TELEGRAM_BOT_TOKEN = FAKE_TOKEN
    settings.TELEGRAM_API_PROXY_URL = proxy
    waits = []
    monkeypatch.setattr(command_module.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        command_module.threading.Event,
        "wait",
        lambda self, timeout=None: waits.append(timeout) or False,
    )
    return waits


def test_command_pauses_before_exiting_on_a_hard_refusal(db, settings, monkeypatch):
    waits = _command_setup(settings, monkeypatch)

    def refuse(self, *, once=False):
        raise SingleInstanceError("Telegram API 401: Unauthorized")

    monkeypatch.setattr(command_module.TelegramBotWorker, "run", refuse)
    with pytest.raises(CommandError, match="401"):
        call_command("run_telegram_bot")
    assert waits == [command_module.REFUSAL_PAUSE_SECONDS]

    waits.clear()
    with pytest.raises(CommandError, match="401"):
        call_command("run_telegram_bot", "--once")
    assert waits == []


def test_command_refuses_an_invalid_proxy_setting_without_echoing_it(db, settings, monkeypatch):
    waits = _command_setup(settings, monkeypatch, proxy="http://user:hunter2@10.231.0.1:2081")

    with pytest.raises(CommandError) as error:
        call_command("run_telegram_bot")

    assert "hunter2" not in str(error.value)
    assert waits == [command_module.REFUSAL_PAUSE_SECONDS]


def test_public_runtime_never_configures_the_telegram_proxy():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "config/settings/public.py"
    ).read_text(encoding="utf-8")
    assert 'TELEGRAM_API_PROXY_URL = ""' in source
