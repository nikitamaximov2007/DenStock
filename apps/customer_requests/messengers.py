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
# https://host[:port] for production; a local origin only for tests and previews.
DEEP_LINK_ORIGIN_RE = re.compile(
    r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?$|^http://(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?$"
)
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class MessengerLinkError(ValueError):
    """Safe, non-enumerating failure for a deep-link operation."""


@dataclass(frozen=True, slots=True)
class IssuedMessengerLink:
    token: str
    expires_at: object


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _link_ttl(channel: str) -> timedelta:
    seconds = {
        CustomerRequestMessengerLinkToken.Channel.TELEGRAM: (
            settings.TELEGRAM_REQUEST_LINK_TTL_SECONDS
        ),
        CustomerRequestMessengerLinkToken.Channel.MAX: settings.MAX_REQUEST_LINK_TTL_SECONDS,
    }.get(channel)
    if seconds is None:
        raise MessengerLinkError("Неизвестный мессенджер.")
    if not 60 <= seconds <= 7 * 24 * 60 * 60:
        raise MessengerLinkError("Срок действия ссылки настроен небезопасно.")
    return timedelta(seconds=seconds)


@transaction.atomic
def issue_messenger_link(*, request_id: int, channel: str, by=None) -> IssuedMessengerLink:
    """Issue one fresh link and revoke prior unused links for its channel."""
    request = CustomerRequest.objects.select_for_update().get(pk=request_id)
    if request.status == CustomerRequest.Status.CANCELED:
        raise MessengerLinkError("Для отменённой заявки нельзя создать ссылку.")
    if request.consent_withdrawn_at is not None or request.data_anonymized_at is not None:
        # A new link would store the customer's chat identity again.
        raise MessengerLinkError("Клиент отозвал согласие на связь: ссылку создать нельзя.")
    if channel not in CustomerRequestMessengerLinkToken.Channel.values:
        raise MessengerLinkError("Неизвестный мессенджер.")
    if request.preferred_messenger != channel:
        raise MessengerLinkError("Для этой заявки выбран другой мессенджер.")
    if channel == CustomerRequestMessengerLinkToken.Channel.TELEGRAM:
        from .models import TelegramConversation

        # Requests created before Telegram messaging get their conversation here.
        TelegramConversation.objects.get_or_create(request=request)
    now = timezone.now()
    CustomerRequestMessengerLinkToken.objects.filter(
        request=request,
        channel=channel,
        used_at__isnull=True,
        revoked_at__isnull=True,
    ).update(revoked_at=now)
    for _ in range(3):
        token = secrets.token_urlsafe(32)
        try:
            row = CustomerRequestMessengerLinkToken.objects.create(
                request=request,
                channel=channel,
                token_hash=_token_hash(token),
                expires_at=now + _link_ttl(channel),
                created_by=by,
            )
        except IntegrityError:
            continue
        return IssuedMessengerLink(token=token, expires_at=row.expires_at)
    raise MessengerLinkError("Не удалось создать безопасную ссылку. Повторите попытку.")


def issue_telegram_link(*, request_id: int, by=None) -> IssuedMessengerLink:
    return issue_messenger_link(
        request_id=request_id,
        channel=CustomerRequestMessengerLinkToken.Channel.TELEGRAM,
        by=by,
    )


def issue_initial_telegram_link(request: CustomerRequest) -> str:
    """The first link of a request created a moment ago, in the same transaction.

    INSERT only: the public database role cannot lock or update requests, and a
    brand-new request has no earlier link to revoke. Returns the raw token for
    the customer's success page, or "" when the lifetime is misconfigured, so
    a Telegram setting can never cost the customer the request itself.
    """
    channel = CustomerRequestMessengerLinkToken.Channel.TELEGRAM
    try:
        ttl = _link_ttl(channel)
    except MessengerLinkError:
        return ""
    token = secrets.token_urlsafe(32)
    CustomerRequestMessengerLinkToken.objects.create(
        request=request,
        channel=channel,
        token_hash=_token_hash(token),
        expires_at=timezone.now() + ttl,
    )
    return token


def issue_max_link(*, request_id: int, by=None) -> IssuedMessengerLink:
    return issue_messenger_link(
        request_id=request_id,
        channel=CustomerRequestMessengerLinkToken.Channel.MAX,
        by=by,
    )


def telegram_deep_link_origin() -> str:
    """Origin of the customer's deep link, or "" when it is not usable.

    The success page needs it verbatim for its Content-Security-Policy: a
    browser blocks the POST -> redirect handoff unless the target origin is an
    allowed form-action source.
    """
    origin = str(settings.TELEGRAM_DEEP_LINK_BASE_URL or "").strip().rstrip("/")
    return origin if DEEP_LINK_ORIGIN_RE.fullmatch(origin) else ""


def telegram_start_url(token: str) -> str | None:
    """Return the documented Bot API deep-link representation, if configured."""
    username = settings.TELEGRAM_BOT_USERNAME
    origin = telegram_deep_link_origin()
    if not origin or not TELEGRAM_USERNAME_RE.fullmatch(username):
        return None
    if not TOKEN_RE.fullmatch(token):
        raise MessengerLinkError("Некорректная ссылка Telegram.")
    return f"{origin}/{username}?start={token}"


@transaction.atomic
def consume_messenger_start(
    *,
    channel: str,
    token: str,
    chat_id: int | str,
    user_id: int | None = None,
    username: str = "",
) -> CustomerRequest:
    """Consume one valid token after a user has explicitly started a channel."""
    if channel not in CustomerRequestMessengerLinkToken.Channel.values:
        raise MessengerLinkError("Ссылка недействительна или уже использована.")
    if not TOKEN_RE.fullmatch(str(token or "")):
        raise MessengerLinkError("Ссылка недействительна или уже использована.")
    chat_id = str(chat_id or "").strip()
    if not chat_id or len(chat_id) > 64:
        raise MessengerLinkError("Ссылка недействительна или уже использована.")
    if channel == CustomerRequestMessengerLinkToken.Channel.TELEGRAM and not re.fullmatch(
        r"-?[0-9]{1,20}", chat_id
    ):
        raise MessengerLinkError("Ссылка недействительна или уже использована.")
    now = timezone.now()
    try:
        row = (
            CustomerRequestMessengerLinkToken.objects.select_for_update()
            .select_related("request")
            .get(
                channel=channel,
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
        or row.request.consent_withdrawn_at is not None
        or row.request.data_anonymized_at is not None
    ):
        raise MessengerLinkError("Ссылка недействительна или уже использована.")
    try:
        CustomerRequestMessengerContact.objects.update_or_create(
            request=row.request,
            defaults={
                "channel": channel,
                "remote_chat_id": chat_id,
            },
        )
    except IntegrityError as exc:
        raise MessengerLinkError("Этот чат уже связан с другой заявкой.") from exc
    row.used_at = now
    row.save(update_fields=["used_at"])
    # A retried handoff may have left earlier links unused. Once one of them
    # binds the chat, the rest must never bind this request again.
    CustomerRequestMessengerLinkToken.objects.filter(
        request=row.request, channel=channel, used_at__isnull=True, revoked_at__isnull=True
    ).exclude(pk=row.pk).update(revoked_at=now)
    if channel == CustomerRequestMessengerLinkToken.Channel.TELEGRAM:
        from .telegram_service import bind_customer_chat

        bind_customer_chat(
            request=row.request,
            chat_id=int(chat_id),
            user_id=user_id,
            username=username,
            link_token_id=row.pk,
        )
    return row.request


def consume_telegram_start(
    *, token: str, chat_id: int | str, user_id: int | None = None, username: str = ""
) -> CustomerRequest:
    return consume_messenger_start(
        channel=CustomerRequestMessengerLinkToken.Channel.TELEGRAM,
        token=token,
        chat_id=chat_id,
        user_id=user_id,
        username=username,
    )


def consume_max_start(*, token: str, chat_id: int | str) -> CustomerRequest:
    return consume_messenger_start(
        channel=CustomerRequestMessengerLinkToken.Channel.MAX,
        token=token,
        chat_id=chat_id,
    )
