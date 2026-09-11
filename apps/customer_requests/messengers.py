"""Provider-neutral link-token mechanics for request messenger channels."""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    CustomerRequest,
    CustomerRequestMessengerContact,
    CustomerRequestMessengerLinkToken,
)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class MessengerLinkError(ValueError):
    """Safe, non-enumerating failure for a deep-link operation."""


@dataclass(frozen=True, slots=True)
class IssuedMessengerLink:
    token: str
    expires_at: object


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _link_ttl() -> timedelta:
    seconds = settings.TELEGRAM_REQUEST_LINK_TTL_SECONDS
    if not 60 <= seconds <= 7 * 24 * 60 * 60:
        raise MessengerLinkError("Срок действия ссылки настроен небезопасно.")
    return timedelta(seconds=seconds)


@transaction.atomic
def issue_telegram_link(*, request_id: int, by=None) -> IssuedMessengerLink:
    """Issue one fresh link and revoke prior unused Telegram links."""
    request = CustomerRequest.objects.select_for_update().get(pk=request_id)
    if request.status == CustomerRequest.Status.CANCELED:
        raise MessengerLinkError("Для отменённой заявки нельзя создать ссылку.")
    if request.preferred_messenger != CustomerRequest.Messenger.TELEGRAM:
        raise MessengerLinkError("Для этой заявки выбран другой мессенджер.")
    now = timezone.now()
    CustomerRequestMessengerLinkToken.objects.filter(
        request=request,
        channel=CustomerRequestMessengerLinkToken.Channel.TELEGRAM,
        used_at__isnull=True,
        revoked_at__isnull=True,
    ).update(revoked_at=now)
    for _ in range(3):
        token = secrets.token_urlsafe(32)
        try:
            row = CustomerRequestMessengerLinkToken.objects.create(
                request=request,
                channel=CustomerRequestMessengerLinkToken.Channel.TELEGRAM,
                token_hash=_token_hash(token),
                expires_at=now + _link_ttl(),
                created_by=by,
            )
        except IntegrityError:
            continue
        return IssuedMessengerLink(token=token, expires_at=row.expires_at)
    raise MessengerLinkError("Не удалось создать безопасную ссылку. Повторите попытку.")


def telegram_start_url(token: str) -> str | None:
    """Return the documented Bot API deep-link representation, if configured."""
    username = settings.TELEGRAM_BOT_USERNAME
    if not TELEGRAM_USERNAME_RE.fullmatch(username):
        return None
    if not TOKEN_RE.fullmatch(token):
        raise MessengerLinkError("Некорректная ссылка Telegram.")
    return f"https://t.me/{username}?start={token}"


@transaction.atomic
def consume_telegram_start(*, token: str, chat_id: int | str) -> CustomerRequest:
    """Consume one valid token after a user has explicitly started the bot."""
    if not TOKEN_RE.fullmatch(str(token or "")):
        raise MessengerLinkError("Ссылка недействительна или уже использована.")
    chat_id = str(chat_id or "").strip()
    if not chat_id or len(chat_id) > 64:
        raise MessengerLinkError("Ссылка недействительна или уже использована.")
    now = timezone.now()
    try:
        row = (
            CustomerRequestMessengerLinkToken.objects.select_for_update()
            .select_related("request")
            .get(
                channel=CustomerRequestMessengerLinkToken.Channel.TELEGRAM,
                token_hash=_token_hash(token),
            )
        )
    except CustomerRequestMessengerLinkToken.DoesNotExist as exc:
        raise MessengerLinkError("Ссылка недействительна или уже использована.") from exc
    if (
        row.used_at is not None
        or row.revoked_at is not None
        or row.expires_at <= now
        or row.request.status == CustomerRequest.Status.CANCELED
    ):
        raise MessengerLinkError("Ссылка недействительна или уже использована.")
    try:
        CustomerRequestMessengerContact.objects.update_or_create(
            request=row.request,
            defaults={
                "channel": CustomerRequestMessengerContact.Channel.TELEGRAM,
                "remote_chat_id": chat_id,
            },
        )
    except IntegrityError as exc:
        raise MessengerLinkError("Этот чат уже связан с другой заявкой.") from exc
    row.used_at = now
    row.save(update_fields=["used_at"])
    return row.request
