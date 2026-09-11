import json
from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.customer_requests.messengers import (
    MessengerLinkError,
    consume_telegram_start,
    issue_telegram_link,
    telegram_start_url,
)
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestMessengerContact,
    CustomerRequestMessengerLinkToken,
)
from apps.customer_requests.telegram import TelegramProvider, handle_update

from .test_customer_requests import _create


@pytest.fixture
def part(db):
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    unit = Unit.objects.get(name="Штука")
    manufacturer, _ = Manufacturer.objects.get_or_create(name="BRP")
    result = PartType.objects.create(
        name="РЕМЕНЬ ПРИВОДНОЙ",
        category=category,
        unit=unit,
        manufacturer=manufacturer,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=result, value="448", is_primary=True)
    return result


class RecordingProvider(TelegramProvider):
    def __init__(self):
        self.chat_ids = []

    def send_start_acknowledgement(self, *, chat_id: str) -> None:
        self.chat_ids.append(chat_id)


def test_telegram_link_is_opaque_short_lived_and_consumed_once(part):
    request, _ = _create(part=part)
    issued = issue_telegram_link(request_id=request.pk)

    assert len(issued.token) >= 32
    assert issued.token != str(request.pk)
    assert not issued.token.startswith(f"{request.pk}.")
    stored = CustomerRequestMessengerLinkToken.objects.get()
    assert stored.token_hash != issued.token
    assert stored.used_at is None

    linked = consume_telegram_start(token=issued.token, chat_id=123456)

    assert linked.pk == request.pk
    assert CustomerRequestMessengerContact.objects.get().remote_chat_id == "123456"
    stored.refresh_from_db()
    assert stored.used_at is not None
    with pytest.raises(MessengerLinkError, match="недействительна"):
        consume_telegram_start(token=issued.token, chat_id=123456)


def test_expired_revoked_and_cancelled_links_are_rejected(part):
    request, _ = _create(part=part)
    expired = issue_telegram_link(request_id=request.pk)
    row = CustomerRequestMessengerLinkToken.objects.get()
    row.expires_at = timezone.now() - timedelta(seconds=1)
    row.save(update_fields=["expires_at"])
    with pytest.raises(MessengerLinkError, match="недействительна"):
        consume_telegram_start(token=expired.token, chat_id=1)

    active = issue_telegram_link(request_id=request.pk)
    request.status = CustomerRequest.Status.CANCELED
    request.save(update_fields=["status"])
    with pytest.raises(MessengerLinkError, match="недействительна"):
        consume_telegram_start(token=active.token, chat_id=1)
    with pytest.raises(MessengerLinkError, match="отменённой"):
        issue_telegram_link(request_id=request.pk)


def test_reissuing_link_revokes_the_previous_one(part):
    request, _ = _create(part=part)
    first = issue_telegram_link(request_id=request.pk)
    second = issue_telegram_link(request_id=request.pk)

    with pytest.raises(MessengerLinkError, match="недействительна"):
        consume_telegram_start(token=first.token, chat_id=1)
    consume_telegram_start(token=second.token, chat_id=1)


@override_settings(TELEGRAM_BOT_USERNAME="DenisStockBot")
def test_telegram_deep_link_uses_only_the_opaque_start_token(part):
    request, _ = _create(part=part)
    issued = issue_telegram_link(request_id=request.pk)

    url = telegram_start_url(issued.token)

    assert url == f"https://t.me/DenisStockBot?start={issued.token}"
    assert f"start={request.pk}" not in url


@override_settings(TELEGRAM_WEBHOOK_SECRET="secret-for-test")
def test_webhook_accepts_started_user_and_never_echoes_request_data(client, part):
    request, _ = _create(part=part)
    issued = issue_telegram_link(request_id=request.pk)
    payload = {"message": {"chat": {"id": 9988}, "text": f"/start {issued.token}"}}

    rejected = client.post(
        reverse("customer_request_telegram_webhook"),
        data="{}",
        content_type="application/json",
    )
    accepted = client.post(
        reverse("customer_request_telegram_webhook"),
        data=json.dumps(payload),
        content_type="application/json",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-for-test"},
    )

    assert rejected.status_code == 404
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True, "accepted": True}
    assert request.customer_phone not in accepted.content.decode()
    assert request.comment not in accepted.content.decode()


def test_malformed_update_and_unknown_token_are_bounded(part):
    _create(part=part)
    provider = RecordingProvider()

    malformed = handle_update({"message": {"chat": {}, "text": "/start"}}, provider=provider)
    unknown = handle_update(
        {"message": {"chat": {"id": 1}, "text": f"/start {'z' * 32}"}}, provider=provider
    )

    assert malformed.accepted is False
    assert unknown.accepted is False
    assert provider.chat_ids == []
