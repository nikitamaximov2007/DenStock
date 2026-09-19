"""An employee's reply to a customer, whichever messenger that customer uses.

DenisStock and the operators' Telegram bot both answer through here. The
employee writes one text; this module finds the request's own messenger and
stores the reply in that transport's rows, where its worker delivers it as the
PRO-STOR bot. What differs per transport stays in its rows: Telegram numbers
bot updates, MAX has none, and each has its own delivery states.

Three things hold for every reply, whatever its channel:

* it is stored at most once per submission. A DenisStock form carries a fresh
  32-hex key; a reply typed in the bot is identified by its Telegram update.
  The request row is locked first, so a double click waits and then finds the
  reply the first click stored; the unique keys are the backstop;
* it is attributed to the real DenisStock user, never shown to the customer;
* it goes only to a request the customer may still write about: an open status,
  consent kept, and a linked messenger. Server state decides, not the page or
  the button the employee happened to press.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from . import messaging
from .attachments import AttachmentError, validate_attachment
from .max_api import MAX_TEXT_CHARS
from .models import (
    CustomerRequest,
    MaxConversation,
    MaxDeliveryStatus,
    MaxMessage,
    MaxOutboxEvent,
    TelegramConversation,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramOutboxEvent,
)

TELEGRAM_TEXT_CHARS = 4000
FORM_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
CHANNEL_LABELS = {
    CustomerRequest.Messenger.TELEGRAM: "Telegram",
    CustomerRequest.Messenger.MAX: "MAX",
}


class OperatorReplyError(ValueError):
    """A reply that was not queued, with a reason safe to show the employee."""


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """Where a reply to this request would go, and whether it may go there now."""

    request: CustomerRequest
    channel: str
    conversation: TelegramConversation | MaxConversation | None

    @property
    def label(self) -> str:
        return CHANNEL_LABELS.get(self.channel, self.channel)

    @property
    def linked(self) -> bool:
        return self.conversation is not None and self.conversation.is_linked

    def blocked_reason(self) -> str:
        """Why an employee cannot answer this customer now, or "" if they can."""
        request = self.request
        reference = request.reference
        if request.status not in messaging.MESSAGEABLE_STATUSES:
            return f"Заявка №{reference} уже закрыта."
        if not messaging.customer_contact_allowed(request):
            return f"По заявке №{reference} клиент отозвал согласие на связь."
        if not self.linked:
            return (
                f"Клиент ещё не подключил {self.label} к заявке №{reference}. "
                "Свяжитесь по телефону."
            )
        return ""


def reply_target(request: CustomerRequest) -> ReplyTarget:
    """The request's own messenger: the one the customer chose and linked.

    A link can only be issued for the preferred messenger, so a request never
    has a linked conversation in the other one.
    """
    channel = request.preferred_messenger
    model = MaxConversation if channel == CustomerRequest.Messenger.MAX else TelegramConversation
    conversation = model.objects.filter(request=request).first()
    return ReplyTarget(request=request, channel=channel, conversation=conversation)


@dataclass(frozen=True, slots=True)
class ReplyResult:
    message: TelegramMessage | MaxMessage
    channel: str
    created: bool

    @property
    def label(self) -> str:
        return CHANNEL_LABELS.get(self.channel, self.channel)


def _dedupe_key(key: str) -> str:
    return f"operator_reply:{key}"


def _stored_reply(key: str, telegram_update_id: int | None):
    dedupe_key = _dedupe_key(key)
    message = MaxMessage.objects.filter(dedupe_key=dedupe_key).first()
    if message is not None:
        return ReplyResult(message, CustomerRequest.Messenger.MAX, created=False)
    lookup = TelegramMessage.objects.filter(dedupe_key=dedupe_key)
    if telegram_update_id is not None:
        lookup = TelegramMessage.objects.filter(telegram_update_id=telegram_update_id)
    message = lookup.first()
    if message is not None:
        return ReplyResult(message, CustomerRequest.Messenger.TELEGRAM, created=False)
    return None


def _may_reply(user) -> bool:
    return bool(user and user.is_active and getattr(user, "can_manage_sales", False))


@transaction.atomic
def submit_reply(
    *,
    request_id: int,
    user,
    text: str,
    key: str,
    telegram_operator=None,
    telegram_update_id: int | None = None,
    channel: str | None = None,
    attachment=None,
) -> ReplyResult:
    """Queue one reply for delivery by the request's own messenger worker.

    ``key`` is a DenisStock form key (32 hex characters) or ``tg:<update id>``
    for a reply typed in the operators' bot. ``channel``, when given, refuses a
    request of the other messenger instead of answering there.
    """
    if not _may_reply(user):
        raise OperatorReplyError("Недостаточно прав для ответа клиенту.")
    key = str(key or "").strip()
    if telegram_update_id is not None:
        if key != f"tg:{telegram_update_id}":
            raise OperatorReplyError("Ответ не распознан. Повторите.")
    elif not FORM_KEY_RE.fullmatch(key):
        raise OperatorReplyError("Форма устарела. Обновите страницу и повторите.")
    stored = _stored_reply(key, telegram_update_id)
    if stored is not None:
        return stored
    try:
        request = CustomerRequest.objects.select_for_update().get(pk=request_id)
    except CustomerRequest.DoesNotExist:
        raise OperatorReplyError("Заявка не найдена.") from None
    # A concurrent submission of the same key waited on the lock above.
    stored = _stored_reply(key, telegram_update_id)
    if stored is not None:
        return stored
    target = reply_target(request)
    if channel is not None and target.channel != channel:
        label = CHANNEL_LABELS.get(channel, channel)
        raise OperatorReplyError(
            f"Клиент ещё не подключил {label} к заявке. Свяжитесь по телефону."
        )
    reason = target.blocked_reason()
    if reason:
        raise OperatorReplyError(f"{reason} Сообщение не отправлено.")
    text = (text or "").strip()
    validated = None
    if attachment is not None:
        try:
            validated = validate_attachment(attachment)
        except AttachmentError as exc:
            raise OperatorReplyError(str(exc)) from None
    is_max = target.channel == CustomerRequest.Messenger.MAX
    limit = MAX_TEXT_CHARS if is_max else TELEGRAM_TEXT_CHARS
    if not text and validated is None:
        raise OperatorReplyError("Пустое сообщение не отправлено.")
    if len(text) > limit:
        raise OperatorReplyError(f"Сообщение длиннее {limit} символов. Сократите его.")
    if is_max:
        if validated is not None:
            raise OperatorReplyError("Вложения пока недоступны в MAX.")
        message = _queue_max_reply(target, user=user, text=text, key=key)
    else:
        message = _queue_telegram_reply(
            target,
            user=user,
            text=text,
            key=key,
            telegram_operator=telegram_operator,
            telegram_update_id=telegram_update_id,
            attachment=validated,
        )
    return ReplyResult(message, target.channel, created=True)


def _queue_max_reply(target: ReplyTarget, *, user, text: str, key: str) -> MaxMessage:
    conversation = MaxConversation.objects.select_for_update().get(pk=target.conversation.pk)
    now = timezone.now()
    message = MaxMessage.objects.create(
        conversation=conversation,
        direction=MaxMessage.Direction.OPERATOR,
        text=text,
        recipient_chat_id=conversation.customer_chat_id,
        delivery_status=MaxDeliveryStatus.PENDING,
        next_attempt_at=now,
        dedupe_key=_dedupe_key(key),
        operator_user=user,
    )
    conversation.last_message_at = now
    conversation.save(update_fields=["last_message_at", "updated_at"])
    MaxOutboxEvent.objects.create(
        kind=MaxOutboxEvent.Kind.OPERATOR_REPLY,
        request=target.request,
        message=message,
        exclude_user=user,
        dedupe_key=f"operator_reply:{message.pk}",
        next_attempt_at=now,
    )
    return message


def _queue_telegram_reply(
    target: ReplyTarget, *, user, text: str, key: str, telegram_operator, telegram_update_id,
    attachment=None,
) -> TelegramMessage:
    conversation = TelegramConversation.objects.select_for_update().get(pk=target.conversation.pk)
    # The author's own bot account is never told about their own reply.
    operator = telegram_operator or getattr(user, "telegram_operator", None)
    now = timezone.now()
    message = TelegramMessage.objects.create(
        conversation=conversation,
        direction=TelegramMessage.Direction.OPERATOR,
        text=text,
        delivery_status=TelegramDeliveryStatus.PENDING,
        next_attempt_at=now,
        telegram_update_id=telegram_update_id,
        dedupe_key="" if telegram_update_id is not None else _dedupe_key(key),
        operator=operator,
        operator_user=user,
        attachment_name=attachment.filename if attachment else "",
        attachment_content_type=attachment.content_type if attachment else "",
    )
    if attachment:
        message.attachment.save(attachment.filename, ContentFile(attachment.content), save=False)
        message.save(update_fields=["attachment"])
    conversation.last_message_at = now
    conversation.save(update_fields=["last_message_at", "updated_at"])
    TelegramOutboxEvent.objects.create(
        kind=TelegramOutboxEvent.Kind.OPERATOR_REPLY,
        request=target.request,
        message=message,
        exclude_operator=operator,
        dedupe_key=f"operator_reply:{message.pk}",
        next_attempt_at=now,
    )
    return message
