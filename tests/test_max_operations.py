"""MAX operations surface: commands, fail-closed settings, runtime isolation, redaction.

Everything talks to ``FakeMaxServer``; no real token, secret or network.
"""

from io import StringIO
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import Resolver404, get_resolver

from apps.core.observability import redact
from apps.customer_requests.models import MaxDeliveryStatus, MaxMessage

from .max_fake import FAKE_MAX_TOKEN, FAKE_WEBHOOK_SECRET, FakeMaxServer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server():
    fake = FakeMaxServer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def configured(settings, server):
    settings.MAX_BOT_TOKEN = FAKE_MAX_TOKEN
    settings.MAX_API_BASE_URL = server.base_url
    settings.MAX_API_TIMEOUT_SECONDS = 2
    settings.MAX_WEBHOOK_SECRET = FAKE_WEBHOOK_SECRET
    settings.MAX_PUBLIC_WEBHOOK_URL = "https://prostor.example/customer-requests/max/webhook/"
    settings.MAX_BOT_HEARTBEAT_FILE = ""
    return settings


def test_defaults_fail_closed():
    base = (ROOT / "config/settings/base.py").read_text(encoding="utf-8")
    assert 'env.bool("MAX_WEBHOOK_ENABLED", default=False)' in base
    assert 'env("MAX_BOT_TOKEN", default="")' in base
    assert 'env("MAX_WEBHOOK_SECRET", default="")' in base
    assert 'env("MAX_PUBLIC_WEBHOOK_URL", default="")' in base


def test_public_runtime_holds_no_max_secret_and_routes_no_webhook():
    public = (ROOT / "config/settings/public.py").read_text(encoding="utf-8")
    assert 'MAX_BOT_TOKEN = ""' in public
    assert 'MAX_WEBHOOK_SECRET = ""' in public
    assert "MAX_WEBHOOK_ENABLED = False" in public
    resolver = get_resolver("config.public_urls")
    for path in ("/customer-requests/max/webhook/", "/max/webhook/"):
        with pytest.raises(Resolver404):
            resolver.resolve(path)
    assert get_resolver("config.urls").resolve("/customer-requests/max/webhook/")


def test_logs_redact_authorization_and_the_webhook_secret_in_any_shape():
    shapes = [
        f"Authorization: {FAKE_MAX_TOKEN}",
        f"X-Max-Bot-Api-Secret: {FAKE_WEBHOOK_SECRET}",
        f"headers={{'Authorization': '{FAKE_MAX_TOKEN}', "
        f"'X-Max-Bot-Api-Secret': '{FAKE_WEBHOOK_SECRET}'}}",
        f'{{"authorization": "{FAKE_MAX_TOKEN}", "secret": "{FAKE_WEBHOOK_SECRET}"}}',
        f"MAX_BOT_TOKEN={FAKE_MAX_TOKEN} MAX_WEBHOOK_SECRET={FAKE_WEBHOOK_SECRET}",
    ]
    for text in shapes:
        hidden = redact(text)
        assert FAKE_MAX_TOKEN not in hidden, text
        assert FAKE_WEBHOOK_SECRET not in hidden, text


def test_run_max_bot_refuses_without_a_token(db, settings):
    settings.MAX_BOT_TOKEN = ""
    with pytest.raises(CommandError, match="не настроен"):
        call_command("run_max_bot", "--once")


def test_run_max_bot_once_sends_the_outbox(db, configured, server):
    MaxMessage.objects.create(
        direction=MaxMessage.Direction.SYSTEM,
        text="Проверка",
        recipient_chat_id=777,
        dedupe_key="probe",
        delivery_status=MaxDeliveryStatus.PENDING,
        next_attempt_at="2000-01-01T00:00:00Z",
    )
    call_command("run_max_bot", "--once")
    assert server.texts_to(777) == ["Проверка"]
    assert MaxMessage.objects.get().delivery_status == MaxDeliveryStatus.SENT


def test_run_max_bot_refuses_a_rejected_token(db, configured, server, caplog):
    configured.MAX_BOT_TOKEN = "fake-rejected-token-for-tests"
    with pytest.raises(CommandError, match="401"):
        call_command("run_max_bot", "--once")
    assert "fake-rejected-token-for-tests" not in caplog.text


def test_max_webhook_status_reads_and_prints_no_secret(db, configured, server):
    out = StringIO()
    call_command("max_webhook", "status", stdout=out)
    text = out.getvalue()
    assert "subscriptions: 0" in text
    assert FAKE_MAX_TOKEN not in text and FAKE_WEBHOOK_SECRET not in text
    assert {item["method"] for item in server.requests} == {"GET"}


def test_max_webhook_changes_need_confirmation(db, configured, server):
    for action in ("subscribe", "unsubscribe"):
        with pytest.raises(CommandError, match="--confirm"):
            call_command("max_webhook", action)
    assert server.requests == []


def test_max_webhook_subscribe_and_unsubscribe(db, configured, server):
    out = StringIO()
    call_command("max_webhook", "subscribe", "--confirm", stdout=out)
    registered = server.subscriptions
    assert [item["url"] for item in registered] == [configured.MAX_PUBLIC_WEBHOOK_URL]
    assert set(registered[0]["update_types"]) == {
        "bot_started", "bot_stopped", "message_created", "message_callback"
    }
    posted = next(r for r in server.requests if r["method"] == "POST")
    assert posted["body"]["secret"] == FAKE_WEBHOOK_SECRET
    assert "(this)" in out.getvalue()
    assert FAKE_WEBHOOK_SECRET not in out.getvalue() and FAKE_MAX_TOKEN not in out.getvalue()

    call_command("max_webhook", "unsubscribe", "--confirm", stdout=StringIO())
    assert server.subscriptions == []


@pytest.mark.parametrize(
    ("url", "secret", "message"),
    [
        ("http://prostor.example/hook/", FAKE_WEBHOOK_SECRET, "https"),
        ("https://prostor.example/hook/", "bad secret!", "5-256"),
        ("https://prostor.example/hook/", "", "5-256"),
    ],
)
def test_max_webhook_refuses_an_unsafe_subscription(db, configured, server, url, secret, message):
    configured.MAX_PUBLIC_WEBHOOK_URL = url
    configured.MAX_WEBHOOK_SECRET = secret
    with pytest.raises(CommandError, match=message):
        call_command("max_webhook", "subscribe", "--confirm")
    assert server.subscriptions == []


def test_no_real_looking_max_credentials_in_the_repository():
    fake = (ROOT / "tests/max_fake.py").read_text(encoding="utf-8")
    assert "fake" in FAKE_MAX_TOKEN and "fake" in FAKE_WEBHOOK_SECRET
    assert django_settings.MAX_BOT_TOKEN in ("", FAKE_MAX_TOKEN)
    assert "platform-api2.max.ru" not in fake
