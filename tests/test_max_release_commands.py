"""Stage C release commands of MAX: health, identity, webhook subscription.

All against ``FakeMaxServer``; no real token, secret, CA or network.
"""

import os
import time
from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.utils import timezone

from apps.customer_requests.max_api import MaxBotApi
from apps.customer_requests.max_bot import MaxBotWorker
from apps.operations.models import MaxBotRuntime

from .max_fake import FAKE_BOT_USERNAME, FAKE_MAX_TOKEN, FAKE_WEBHOOK_SECRET, FakeMaxServer
from .max_tls import make_local_ca

HOOK = "https://pro-brp.example/customer-requests/max/webhook/"


@pytest.fixture
def server():
    fake = FakeMaxServer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def configured(settings, server, tmp_path):
    settings.MAX_BOT_TOKEN = FAKE_MAX_TOKEN
    settings.MAX_API_BASE_URL = server.base_url
    settings.MAX_API_TIMEOUT_SECONDS = 2
    settings.MAX_WEBHOOK_SECRET = ""  # max-bot never holds it
    settings.MAX_PUBLIC_WEBHOOK_URL = HOOK
    settings.MAX_BOT_HEARTBEAT_FILE = str(tmp_path / "max.heartbeat")
    settings.MAX_API_CA_FILE = ""
    settings.MAX_API_CA_SHA256 = ""
    return settings


def run(*args):
    out = StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


def _secret_file(tmp_path, secret=FAKE_WEBHOOK_SECRET):
    path = tmp_path / ".env.max-webhook"
    path.write_text(f"MAX_WEBHOOK_ENABLED=true\nMAX_WEBHOOK_SECRET={secret}\n")
    return str(path)


# --- max_bot_health ----------------------------------------------------------------------


def _started_worker(configured, server):
    worker = MaxBotWorker(
        MaxBotApi(FAKE_MAX_TOKEN, base_url=server.base_url, timeout=2),
        heartbeat_file=configured.MAX_BOT_HEARTBEAT_FILE,
    )
    worker.start()
    return worker


def test_health_is_ok_for_a_running_worker(db, configured, server):
    worker = _started_worker(configured, server)
    worker.iterate()
    assert run("max_bot_health").strip() == "ok"
    with open(configured.MAX_BOT_HEARTBEAT_FILE) as handle:
        assert handle.read() == worker.worker_id


def test_health_fails_without_a_heartbeat(db, configured):
    with pytest.raises(CommandError, match="heartbeat file missing"):
        call_command("max_bot_health")


def test_health_fails_on_a_stale_heartbeat(db, configured, server):
    _started_worker(configured, server)
    old = time.time() - 600
    os.utime(configured.MAX_BOT_HEARTBEAT_FILE, (old, old))
    with pytest.raises(CommandError, match="heartbeat stale"):
        call_command("max_bot_health")


def test_health_fails_when_the_lease_is_expired_or_not_ours(db, configured, server):
    worker = _started_worker(configured, server)
    MaxBotRuntime.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(CommandError, match="lease expired"):
        call_command("max_bot_health")
    MaxBotRuntime.objects.update(
        worker_id="f" * 32, lease_expires_at=timezone.now() + timedelta(seconds=60)
    )
    with pytest.raises(CommandError, match="another worker"):
        call_command("max_bot_health")
    worker.release()


def test_health_fails_on_a_heartbeat_naming_no_worker(db, configured):
    with open(configured.MAX_BOT_HEARTBEAT_FILE, "w") as handle:
        handle.write("")
    with pytest.raises(CommandError, match="names no worker"):
        call_command("max_bot_health")


def test_health_reports_a_database_outage_without_details(db, configured, server):
    _started_worker(configured, server)
    with mock.patch(
        "apps.customer_requests.management.commands.max_bot_health.health_problems",
        side_effect=DatabaseError("password=hunter2"),
    ), pytest.raises(CommandError) as caught:
        call_command("max_bot_health")
    assert "database unavailable" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_health_makes_no_max_call(db, configured, server):
    _started_worker(configured, server)
    before = len(server.requests)
    run("max_bot_health")
    assert len(server.requests) == before


# --- max_bot_identity --------------------------------------------------------------------


def test_identity_prints_the_bot_and_never_the_token(db, configured, server):
    out = run("max_bot_identity")
    assert "is_bot: true" in out
    assert f"username: {FAKE_BOT_USERNAME}" in out
    assert "user_id: " in out
    assert FAKE_MAX_TOKEN not in out
    assert [r["path"] for r in server.requests] == ["/me"]


def test_identity_env_line_is_the_public_username_only(db, configured, server):
    assert run("max_bot_identity", "--env-line").strip() == f"MAX_BOT_USERNAME={FAKE_BOT_USERNAME}"


@pytest.mark.parametrize(
    "body",
    [
        {"user_id": 1, "is_bot": False, "username": "id1_bot"},
        {"user_id": 1, "is_bot": True, "username": ""},
        {"user_id": 1, "is_bot": True, "username": "bad name!"},
    ],
)
def test_identity_refuses_a_non_bot_or_unusable_username(db, configured, server, body):
    server.script("/me", ("status", 200, body))
    with pytest.raises(CommandError):
        call_command("max_bot_identity", "--env-line")


def test_identity_refuses_a_rejected_token(db, configured, server):
    configured.MAX_BOT_TOKEN = "fake-rejected-token-for-tests"
    with pytest.raises(CommandError, match="401") as caught:
        call_command("max_bot_identity")
    assert "fake-rejected-token-for-tests" not in str(caught.value)


def test_identity_explains_an_untrusted_max_certificate(db, configured, tmp_path):
    local_ca = make_local_ca(tmp_path / "ca")
    https = FakeMaxServer()
    https.start(tls_context=local_ca.server_context)
    try:
        configured.MAX_API_BASE_URL = https.base_url
        with pytest.raises(CommandError, match="MAX_API_CA_FILE"):
            call_command("max_bot_identity")
        configured.MAX_API_CA_FILE = str(local_ca.ca_file)
        configured.MAX_API_CA_SHA256 = local_ca.ca_sha256
        out = run("max_bot_identity")
        assert f"sha256={local_ca.ca_sha256}" in out
    finally:
        https.stop()


def test_identity_fails_closed_on_a_missing_ca_file(db, configured, tmp_path):
    configured.MAX_API_CA_FILE = str(tmp_path / "absent.pem")
    with pytest.raises(CommandError, match="MAX CA file"):
        call_command("max_bot_identity")


def test_identity_refuses_without_a_token(db, settings):
    settings.MAX_BOT_TOKEN = ""
    with pytest.raises(CommandError, match="MAX_BOT_TOKEN"):
        call_command("max_bot_identity")


# --- max_webhook -------------------------------------------------------------------------


def test_subscribe_reads_the_secret_from_webs_file_and_never_prints_it(
    db, configured, server, tmp_path
):
    out = run("max_webhook", "subscribe", "--confirm", "--secret-file", _secret_file(tmp_path))
    posted = next(r for r in server.requests if r["method"] == "POST")
    assert posted["body"]["secret"] == FAKE_WEBHOOK_SECRET
    assert posted["body"]["url"] == HOOK
    assert FAKE_WEBHOOK_SECRET not in out and FAKE_MAX_TOKEN not in out
    assert f"{HOOK} (this)" in out


def test_subscribe_refuses_a_secret_file_without_the_key_or_unreadable(
    db, configured, server, tmp_path
):
    empty = tmp_path / "empty.env"
    empty.write_text("MAX_WEBHOOK_ENABLED=true\n")
    with pytest.raises(CommandError, match="нет MAX_WEBHOOK_SECRET"):
        call_command("max_webhook", "subscribe", "--confirm", "--secret-file", str(empty))
    with pytest.raises(CommandError, match="недоступен"):
        call_command(
            "max_webhook", "subscribe", "--confirm", "--secret-file", str(tmp_path / "none")
        )
    with pytest.raises(CommandError, match="5-256"):
        call_command(
            "max_webhook", "subscribe", "--confirm",
            "--secret-file", _secret_file(tmp_path, secret="bad secret"),
        )
    assert server.requests == []


def test_repeated_subscribe_is_safe_and_says_so(db, configured, server, tmp_path):
    secret_file = _secret_file(tmp_path)
    run("max_webhook", "subscribe", "--confirm", "--secret-file", secret_file)
    out = run("max_webhook", "subscribe", "--confirm", "--secret-file", secret_file)
    assert "already subscribed" in out
    assert [item["url"] for item in server.subscriptions] == [HOOK]


def test_unsubscribe_of_an_absent_url_changes_nothing(db, configured, server):
    out = run("max_webhook", "unsubscribe", "--confirm")
    assert "nothing to remove" in out
    assert not [r for r in server.requests if r["method"] == "DELETE"]


def test_status_is_read_only_marks_foreign_subscriptions_and_gates(
    db, configured, server, tmp_path
):
    server.subscriptions.append(
        {"url": "https://elsewhere.example/hook", "time": 1, "update_types": [], "version": "1"}
    )
    with pytest.raises(CommandError, match="not subscribed"):
        call_command("max_webhook", "status", "--require-subscribed", stdout=StringIO())
    run("max_webhook", "subscribe", "--confirm", "--secret-file", _secret_file(tmp_path))
    before = len(server.requests)
    out = run("max_webhook", "status", "--require-subscribed")
    assert "(OTHER)" in out and "(this)" in out
    assert {r["method"] for r in server.requests[before:]} == {"GET"}


def test_changes_require_confirmation_and_an_https_url(db, configured, server, tmp_path):
    for action in ("subscribe", "unsubscribe"):
        with pytest.raises(CommandError, match="--confirm"):
            call_command("max_webhook", action)
    with pytest.raises(CommandError, match="https"):
        call_command(
            "max_webhook", "subscribe", "--confirm", "--url", "http://insecure.example/hook",
            "--secret-file", _secret_file(tmp_path),
        )
    assert server.requests == []
