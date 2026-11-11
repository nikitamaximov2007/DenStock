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

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from . import messaging
from .attachments import (
    AttachmentError,
    ValidatedAttachment,
    validate_attachment,
    validate_photo,
)
from .max_api import MAX_TEXT_CHARS
from .models import (
    CustomerRequest,
    MaxConversation,
    MaxDeliveryStatus,
    MaxMessage,
    MaxOutboxEvent,
    OperatorNotification,
    StaffMessengerBinding,
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
OWNER_LABELS = {
    "Денис": "Денис, владелец сервиса PRO-STOR",
    "Рим": "Рим, владелец сервиса PRO-STOR",
}
ADMIN_LABELS = {"NIKITA / ADMIN": "PRO-STOR"}


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


def customer_visible_operator_label(label: str) -> str:
    """Return immutable customer-facing wording for a staff identity."""
    normalized = (label or "").strip()
    return ADMIN_LABELS.get(normalized, OWNER_LABELS.get(normalized, normalized))


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
    operator_control_source: str | None = None,
    operator_author_label: str = "",
    customer_responder_label: str | None = None,
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
    elif not (FORM_KEY_RE.fullmatch(key) or key.startswith("staff:")):
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
            if isinstance(attachment, ValidatedAttachment):
                attachment = ContentFile(attachment.content, name=attachment.filename)
            validated = validate_attachment(attachment)
            if validated.content_type.startswith("image/"):
                validated = validate_photo(ContentFile(
                    validated.content, name=validated.filename
                ))
        except AttachmentError as exc:
            raise OperatorReplyError(str(exc)) from None
    is_max = target.channel == CustomerRequest.Messenger.MAX
    limit = MAX_TEXT_CHARS if is_max else TELEGRAM_TEXT_CHARS
    if not text and validated is None:
        raise OperatorReplyError("Пустое сообщение не отправлено.")
    if len(text) > limit:
        raise OperatorReplyError(f"Сообщение длиннее {limit} символов. Сократите его.")
    if operator_control_source is None:
        operator_control_source = "telegram" if telegram_update_id is not None else "web"
    if not operator_author_label:
        operator_author_label = "PRO-STOR" if operator_control_source == "web" else (
            getattr(user, "full_name", "") or user.get_username()
        )
    if settings.CUSTOMER_STAFF_INTRODUCTIONS_ENABLED:
        operator_author_label = user.full_name or user.get_full_name() or user.get_username()
    stored_operator_author_label = operator_author_label
    if operator_control_source in {"telegram", "max"}:
        if customer_responder_label is None:
            customer_responder_label = customer_visible_operator_label(operator_author_label)
        else:
            customer_responder_label = customer_visible_operator_label(customer_responder_label)
        stored_operator_author_label = OWNER_LABELS.get(
            (operator_author_label or "").strip(), operator_author_label
        )
    # The operator console is an independently staged feature. Its compact
    # authorship metadata must not change the accepted workspace timeline
    # until the owner deliberately enables that feature flag.
    if not settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED:
        operator_control_source = ""
        operator_author_label = ""
        stored_operator_author_label = ""
    if settings.CUSTOMER_STAFF_INTRODUCTIONS_ENABLED:
        _ensure_staff_introduction(target, user=user, control_source=operator_control_source,
                                   telegram_operator=telegram_operator)
    elif operator_control_source in {"telegram", "max"}:
        _ensure_customer_visible_responder(
            target,
            user=user,
            label=customer_responder_label or "PRO-STOR",
            control_source=operator_control_source,
            telegram_operator=telegram_operator,
        )
    if is_max:
        message = _queue_max_reply(
            target, user=user, text=text, key=key, attachment=validated,
            operator_control_source=operator_control_source,
            operator_author_label=stored_operator_author_label,
        )
    else:
        message = _queue_telegram_reply(
            target,
            user=user,
            text=text,
            key=key,
            telegram_operator=telegram_operator,
            telegram_update_id=telegram_update_id,
            attachment=validated,
            operator_control_source=operator_control_source,
            operator_author_label=stored_operator_author_label,
        )
    return ReplyResult(message, target.channel, created=True)


def _staff_intro_key(request_id, user_id):
    return f"operator_reply:staff_intro:{request_id}:{user_id}"


def _ensure_staff_introduction(target, *, user, control_source, telegram_operator=None):
    """One introduction per verified Django identity, under the request lock."""
    model = MaxMessage if target.channel == CustomerRequest.Messenger.MAX else TelegramMessage
    key = _staff_intro_key(target.request.pk, user.pk)
    existing = model.objects.filter(dedupe_key=key).first()
    if existing is not None:
        if existing.delivery_status == "uncertain":
            raise OperatorReplyError("Подключение ожидает проверки доставки. Повтор не выполнен.")
        if existing.delivery_status == "failed":
            existing.delivery_status = "pending"
            existing.next_attempt_at = timezone.now()
            existing.attempts = 0
            existing.last_error = ""
            existing.save(update_fields=["delivery_status", "next_attempt_at", "attempts",
                                         "last_error"])
        return
    label = (user.full_name or user.get_full_name() or user.get_username()).strip()[:80]
    kwargs = dict(target=target, user=user, text=f"К диалогу подключился {label}",
                  key=key.removeprefix("operator_reply:"), attachment=None,
                  operator_control_source=control_source, operator_author_label=label)
    if model is TelegramMessage:
        intro = _queue_telegram_reply(**kwargs, telegram_operator=telegram_operator,
                                      telegram_update_id=None)
    else:
        intro = _queue_max_reply(**kwargs)
    intro.direction = model.Direction.SYSTEM
    intro.save(update_fields=["direction"])


def introduction_delivery_blocker(message):
    """A reply cannot overtake its identity introduction, even after a restart."""
    if (not settings.CUSTOMER_STAFF_INTRODUCTIONS_ENABLED
            or message.direction != type(message).Direction.OPERATOR
            or not message.operator_user_id):
        return ""
    intro = type(message).objects.filter(
        dedupe_key=_staff_intro_key(message.conversation.request_id, message.operator_user_id)
    ).first()
    if intro is None or intro.delivery_status == "sent":
        return ""
    return "failed" if intro.delivery_status in {"failed", "uncertain"} else "pending"


def queue_reply_delivery_notification(message):
    if (not settings.CUSTOMER_STAFF_INTRODUCTIONS_ENABLED
            or not isinstance(message, (TelegramMessage, MaxMessage))
            or message.direction != type(message).Direction.OPERATOR
            or message.operator_control_source not in {"telegram", "max"}
            or message.delivery_status not in {"sent", "failed", "uncertain"}):
        return
    labels = {"sent": "Сообщение отправлено", "failed": "Сообщение не отправлено",
              "uncertain": "Неизвестно, доставлено ли сообщение. Автоповтор отключён"}
    for binding in StaffMessengerBinding.objects.filter(
        user_id=message.operator_user_id, provider=message.operator_control_source, is_active=True
    ):
        OperatorNotification.objects.get_or_create(
            dedupe_key=f"reply-result:{type(message).__name__}:{message.pk}:"
            f"{message.delivery_status}:{binding.pk}",
            defaults={"binding": binding, "request": message.conversation.request,
                      "kind": OperatorNotification.Kind.REPLY_DELIVERY,
                      "preview": labels[message.delivery_status],
                      "next_attempt_at": timezone.now()},
        )


def _ensure_customer_visible_responder(
    target: ReplyTarget, *, user, label: str, control_source: str, telegram_operator=None
) -> None:
    """Record one real introduction when the visible responder changes.

    ``submit_reply`` already holds the request row lock, so two operators cannot
    create competing introductions for the same transition.
    """
    request = target.request
    previous = request.current_responder_label
    if previous == label:
        return
    if request.pending_responder_label:
        if request.pending_responder_label != label:
            raise OperatorReplyError("Смена ответственного ещё не подтверждена доставкой.")
        intro_key = f"intro:{request.pk}:{_dedupe_key(label)}"
        model = MaxMessage if target.channel == CustomerRequest.Messenger.MAX else TelegramMessage
        existing = model.objects.filter(dedupe_key=_dedupe_key(intro_key)).first()
        if existing is not None and existing.delivery_status == (
            MaxDeliveryStatus.UNCERTAIN if target.channel == CustomerRequest.Messenger.MAX
            else TelegramDeliveryStatus.UNCERTAIN
        ):
            raise OperatorReplyError("Введение ожидает проверки доставки. Повтор не выполнен.")
        if existing is None or existing.delivery_status != (
            MaxDeliveryStatus.FAILED if target.channel == CustomerRequest.Messenger.MAX
            else TelegramDeliveryStatus.FAILED
        ):
            raise OperatorReplyError("Введение ещё доставляется. Повторите после статуса доставки.")
        request.pending_responder_label = ""
        request.pending_responder_control_source = ""
        request.save(update_fields=["pending_responder_label", "pending_responder_control_source"])
    text = f"Вам отвечает {label}." if not previous else f"К диалогу подключился {label}."
    key = f"intro:{request.pk}:{_dedupe_key(label)}"
    if request.current_responder_label != label and request.pending_responder_label == "":
        failed_model = (
            MaxMessage if target.channel == CustomerRequest.Messenger.MAX else TelegramMessage
        )
        if failed_model.objects.filter(dedupe_key=_dedupe_key(key)).exists():
            key = f"{key}:retry:{timezone.now().timestamp()}"
    if target.channel == CustomerRequest.Messenger.MAX:
        _queue_max_reply(
            target,
            user=user,
            text=text,
            key=key,
            attachment=None,
            operator_control_source=control_source,
            operator_author_label=label,
        )
    else:
        _queue_telegram_reply(
            target,
            user=user,
            text=text,
            key=key,
            telegram_operator=telegram_operator,
            telegram_update_id=None,
            attachment=None,
            operator_control_source=control_source,
            operator_author_label=label,
        )
    request.pending_responder_label = label[:80]
    request.pending_responder_control_source = control_source[:12]
    request.save(update_fields=["pending_responder_label", "pending_responder_control_source"])


def confirm_responder_transition(message) -> None:
    """Confirm an intro only after its transport reports SENT."""
    if not str(message.dedupe_key or "").startswith("operator_reply:intro:"):
        return
    sent_status = (
        MaxDeliveryStatus.SENT if isinstance(message, MaxMessage) else TelegramDeliveryStatus.SENT
    )
    if message.delivery_status != sent_status:
        return
    request = getattr(getattr(message, "conversation", None), "request", None)
    if request is None or not request.pending_responder_label:
        return
    if request.pending_responder_label != message.operator_author_label:
        return
    request.current_responder_label = request.pending_responder_label
    request.current_responder_control_source = request.pending_responder_control_source
    request.pending_responder_label = ""
    request.pending_responder_control_source = ""
    request.save(update_fields=[
        "current_responder_label", "current_responder_control_source",
        "pending_responder_label", "pending_responder_control_source",
    ])


def _queue_max_reply(
    target: ReplyTarget, *, user, text: str, key: str, attachment=None,
    operator_control_source: str, operator_author_label: str,
) -> MaxMessage:
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
        operator_control_source=operator_control_source,
        operator_author_label=operator_author_label[:80],
        attachment_name=attachment.filename if attachment else "",
        attachment_content_type=attachment.content_type if attachment else "",
    )
    if attachment:
        message.attachment.save(attachment.filename, ContentFile(attachment.content), save=False)
        message.save(update_fields=["attachment"])
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
    attachment=None, operator_control_source: str, operator_author_label: str,
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
        operator_control_source=operator_control_source,
        operator_author_label=operator_author_label[:80],
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
