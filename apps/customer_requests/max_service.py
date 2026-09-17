"""MAX conversations of customer requests: domain rules, no network I/O.

The webhook (``max_bot.handle_update``) calls these functions inside one
database transaction and returns at once; the MAX worker sends what they
stored. Every customer-facing text is a ``MaxMessage`` row with a unique
``dedupe_key``, so a redelivered webhook finds its answer already queued
instead of queueing a second one. Everything operators must hear about is a
``MaxOutboxEvent``.

What a customer notices (summary, acknowledgement, which request a message
belongs to, when contact is allowed) comes from ``messaging``, the same rules
Telegram uses. What is MAX's own lives here: numeric user and dialog
identities, the string ``mid`` of a message, and callback button payloads.
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import messaging, operator_bot, operator_replies, telegram_service
from .max_api import MAX_TEXT_CHARS
from .messengers import token_hash
from .models import (
    MAX_EXTERNAL_ID_LENGTH,
    CustomerRequest,
    CustomerRequestMessengerLinkToken,
    MaxConversation,
    MaxCustomerChat,
    MaxDeliveryStatus,
    MaxMessage,
    MaxOutboxEvent,
)

HEX_RE = re.compile(r"^[0-9a-f]{32}$")
SELECT_PAYLOAD_PREFIX = "s:"
SELECTOR_LIMIT = 20
EPHEMERAL_RETENTION = timedelta(days=1)

LINKED_TEXT = "Готово. MAX подключён к заявке {reference}."
LINK_INVALID_TEXT = (
    "Ссылка недействительна или устарела. Откройте ссылку со страницы заявки ещё раз. "
    "Если ссылки нет, менеджер свяжется с вами по телефону."
)
UNLINKED_GREETING = (
    "Это бот PRO-STOR для связи по заявкам. Чтобы написать менеджеру, нажмите "
    "«Продолжить в MAX» на странице вашей заявки."
)
LINKED_GREETING = (
    "Напишите сообщение, менеджер PRO-STOR ответит здесь. Если у вас несколько заявок, "
    "отправьте /requests, чтобы выбрать нужную."
)
MEDIA_NOT_SUPPORTED_TEXT = telegram_service.MEDIA_NOT_SUPPORTED_TEXT
SELECT_TEXT = "Выберите заявку, по которой хотите написать."
AMBIGUOUS_TEXT = "У вас несколько заявок. Выберите нужную и отправьте сообщение ещё раз."
SELECTED_TEXT = "Выбрана заявка {reference}. Напишите сообщение."
SELECTION_UNAVAILABLE_TEXT = "Эта заявка недоступна. Отправьте /requests, чтобы выбрать другую."
CLOSED_TEXT = "Переписка по этой заявке закрыта."


def summary_policy() -> messaging.SummaryPolicy:
    return messaging.SummaryPolicy(linked_text=LINKED_TEXT, message_limit=MAX_TEXT_CHARS)


def request_summary_messages(request: CustomerRequest) -> list[str]:
    """The shared order summary, sized for MAX."""
    return messaging.request_summary_messages(request, summary_policy())


def valid_external_id(value) -> bool:
    """A MAX ``mid`` is stored whole or not at all: never truncated."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_EXTERNAL_ID_LENGTH
        and value.strip() == value
    )


def queue_message(
    *,
    chat_id: int,
    text: str,
    dedupe_key: str,
    conversation: MaxConversation | None = None,
    buttons=None,
    callback_id: str = "",
    direction: str = MaxMessage.Direction.SYSTEM,
    operator_user=None,
) -> tuple[MaxMessage, bool]:
    """Store one outgoing message exactly once per ``dedupe_key``."""
    return MaxMessage.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={
            "conversation": conversation,
            "direction": direction,
            "text": text,
            "buttons": buttons,
            "callback_id": callback_id[:256],
            "recipient_chat_id": chat_id,
            "delivery_status": MaxDeliveryStatus.PENDING,
            "next_attempt_at": timezone.now(),
            "operator_user": operator_user,
        },
    )


# --- Linking ---------------------------------------------------------------------------


def ensure_conversation(request: CustomerRequest) -> MaxConversation:
    return MaxConversation.objects.get_or_create(request=request)[0]


def bind_customer_chat(
    *, request: CustomerRequest, chat_id: int, user_id: int, link_token_id
) -> MaxConversation:
    """Attach the MAX user that consumed a valid one-time link.

    Runs inside ``consume_messenger_start``'s transaction, after the token row
    is locked and marked used, so two consumers cannot both bind. The summary
    and the operator event are keyed by the link token: this binding can
    produce them once, whatever is redelivered.
    """
    now = timezone.now()
    conversation, _created = MaxConversation.objects.select_for_update().get_or_create(
        request=request
    )
    previous_user = conversation.customer_user_id
    conversation.customer_user_id = user_id
    conversation.customer_chat_id = chat_id
    conversation.status = MaxConversation.Status.LINKED
    conversation.linked_at = now
    conversation.save()
    if previous_user is not None and previous_user != user_id:
        MaxCustomerChat.objects.filter(
            user_id=previous_user, active_conversation=conversation
        ).update(active_conversation=None)
    # The request the customer just opened becomes the target of plain messages.
    MaxCustomerChat.objects.update_or_create(
        user_id=user_id, defaults={"chat_id": chat_id, "active_conversation": conversation}
    )
    for index, text in enumerate(request_summary_messages(request)):
        queue_message(
            chat_id=chat_id,
            text=text,
            dedupe_key=f"summary:{link_token_id}:{index}",
            conversation=conversation,
        )
    MaxOutboxEvent.objects.get_or_create(
        dedupe_key=f"customer_linked:{link_token_id}",
        defaults={
            "kind": MaxOutboxEvent.Kind.CUSTOMER_LINKED,
            "request": request,
            "next_attempt_at": now,
        },
    )
    return conversation


def start_is_replay(*, token: str, user_id: int) -> bool:
    """A redelivered start of a link this very MAX user already consumed.

    MAX has no update id, so a repeated ``bot_started`` is recognised by the
    state it already produced: the token is used and its request is linked to
    the same user. That answer is silence, not "link is invalid".
    """
    if not isinstance(token, str) or not isinstance(user_id, int):
        return False
    try:
        digest = token_hash(token)
    except (UnicodeEncodeError, AttributeError):
        return False
    return CustomerRequestMessengerLinkToken.objects.filter(
        channel=CustomerRequestMessengerLinkToken.Channel.MAX,
        token_hash=digest,
        used_at__isnull=False,
        request__max_conversation__status=MaxConversation.Status.LINKED,
        request__max_conversation__customer_user_id=user_id,
    ).exists()


def anonymize_conversation(request: CustomerRequest) -> None:
    """Privacy minimization mirrors the request: texts and identities are erased."""
    conversation = MaxConversation.objects.filter(request=request).first()
    if conversation is None:
        return
    MaxMessage.objects.filter(conversation=conversation).update(text="", buttons=None)
    MaxCustomerChat.objects.filter(active_conversation=conversation).update(
        active_conversation=None
    )
    user_id = conversation.customer_user_id
    if user_id is not None and not (
        MaxConversation.objects.filter(
            customer_user_id=user_id, status=MaxConversation.Status.LINKED
        )
        .exclude(pk=conversation.pk)
        .exists()
    ):
        MaxCustomerChat.objects.filter(user_id=user_id).delete()
    conversation.customer_user_id = None
    conversation.customer_chat_id = None
    conversation.status = MaxConversation.Status.CLOSED
    conversation.save()


# --- Customer side ---------------------------------------------------------------------


def linked_conversations(user_id: int) -> list[MaxConversation]:
    return list(
        MaxConversation.objects.select_related("request")
        .filter(customer_user_id=user_id, status=MaxConversation.Status.LINKED)
        .order_by("-linked_at", "-pk")[:SELECTOR_LIMIT]
    )


def selector_buttons(conversations) -> list[list[dict]]:
    """One button per request; the payload is the conversation's opaque id."""
    return [
        [
            {
                "text": f"Заявка {conversation.request.reference}",
                "payload": f"{SELECT_PAYLOAD_PREFIX}{conversation.public_id.hex}",
            }
        ]
        for conversation in conversations
    ]


def _closed_reply(request: CustomerRequest, still_open) -> tuple[str, list | None]:
    """What to say about a request the customer can no longer write about.

    A closed status gets the closed-request text and the requests still open.
    Withdrawn consent or anonymization leaves the request itself open, so it
    keeps its own wording and offers nothing to switch to.
    """
    if request.status in messaging.MESSAGEABLE_STATUSES:
        return CLOSED_TEXT, None
    text = messaging.closed_request_text(request.reference, other_open=bool(still_open))
    return text, selector_buttons(still_open) or None


def queue_greeting(*, user_id: int, chat_id: int, dedupe_key: str) -> None:
    linked = linked_conversations(user_id)
    if messaging.open_conversations(linked):
        text = LINKED_GREETING
    elif linked:
        text = messaging.NO_OPEN_REQUESTS_TEXT
    else:
        text = UNLINKED_GREETING
    queue_message(chat_id=chat_id, text=text, dedupe_key=dedupe_key)


def queue_selector(*, user_id: int, chat_id: int, dedupe_key: str) -> None:
    linked = linked_conversations(user_id)
    # A closed request is never offered, whatever an older keyboard still shows.
    conversations = messaging.open_conversations(linked)
    if not conversations:
        text = messaging.NO_OPEN_REQUESTS_TEXT if linked else UNLINKED_GREETING
        queue_message(chat_id=chat_id, text=text, dedupe_key=dedupe_key)
        return
    state = MaxCustomerChat.objects.filter(user_id=user_id).first()
    current = next(
        (c for c in conversations if state and c.pk == state.active_conversation_id), None
    )
    text = SELECT_TEXT
    if current:
        text += f" Сейчас выбрана заявка {current.request.reference}."
    queue_message(
        chat_id=chat_id, text=text, dedupe_key=dedupe_key, buttons=selector_buttons(conversations)
    )


def select_customer_conversation(
    *, user_id: int, chat_id: int, payload: str, callback_id: str, press_key: str
) -> MaxConversation | None:
    """Only a request already bound to this very MAX user can be selected.

    ``callback_id`` is only what MAX needs to stop the button's spinner. MAX
    documents it as the identifier of the keyboard, so two presses on one
    keyboard may share it; ``press_key`` identifies this press and is what a
    redelivery repeats.
    """
    value = payload[len(SELECT_PAYLOAD_PREFIX):] if isinstance(payload, str) else ""
    conversation = None
    if (
        isinstance(payload, str)
        and payload.startswith(SELECT_PAYLOAD_PREFIX)
        and HEX_RE.fullmatch(value)
    ):
        conversation = (
            MaxConversation.objects.select_related("request")
            .filter(
                public_id=uuid.UUID(hex=value),
                customer_user_id=user_id,
                status=MaxConversation.Status.LINKED,
            )
            .first()
        )
    dedupe_key = f"callback:{press_key}"
    if conversation is None:
        queue_message(
            chat_id=chat_id,
            text=SELECTION_UNAVAILABLE_TEXT,
            dedupe_key=dedupe_key,
            callback_id=callback_id,
        )
        return None
    if not messaging.customer_can_message(conversation.request):
        # An old button of a request that has closed since. The customer's
        # current choice stays exactly as it was; they pick an open one.
        text, buttons = _closed_reply(
            conversation.request, messaging.open_conversations(linked_conversations(user_id))
        )
        queue_message(
            chat_id=chat_id,
            text=text,
            dedupe_key=dedupe_key,
            callback_id=callback_id,
            buttons=buttons,
        )
        return None
    if MaxMessage.objects.filter(dedupe_key=dedupe_key).exists():
        return conversation  # a redelivered press: already selected and confirmed
    MaxCustomerChat.objects.update_or_create(
        user_id=user_id, defaults={"chat_id": chat_id, "active_conversation": conversation}
    )
    queue_message(
        chat_id=chat_id,
        text=SELECTED_TEXT.format(reference=conversation.request.reference),
        dedupe_key=dedupe_key,
        conversation=conversation,
        callback_id=callback_id,
    )
    return conversation


def _locked_customer_chat(*, user_id: int, chat_id: int) -> MaxCustomerChat:
    try:
        with transaction.atomic():
            MaxCustomerChat.objects.get_or_create(user_id=user_id, defaults={"chat_id": chat_id})
    except IntegrityError:
        pass  # created concurrently; the lock below waits for it
    return MaxCustomerChat.objects.select_for_update().get(user_id=user_id)


RECORDED = "recorded"
DUPLICATE = "duplicate"
AMBIGUOUS = "ambiguous"
UNLINKED = "unlinked"
CLOSED = "closed"


def record_customer_message(*, user_id: int, chat_id: int, mid: str, text: str) -> str:
    """Store one plain customer message for the right request, exactly once.

    ``mid`` is MAX's own identity of the message. Every message of one MAX user
    serializes on that user's routing row, and the duplicate check happens
    under that lock: a concurrent redelivery waits and then finds the stored
    message. The persisted first message is the durable acknowledgement marker.
    """
    if not valid_external_id(mid):
        raise ValueError("MAX message id is missing or too long.")
    if MaxMessage.objects.filter(
        direction=MaxMessage.Direction.CUSTOMER, external_message_id=mid
    ).exists():
        return DUPLICATE
    conversations = linked_conversations(user_id)
    if not conversations:
        queue_message(chat_id=chat_id, text=UNLINKED_GREETING, dedupe_key=f"reply:{mid}")
        return UNLINKED
    state = _locked_customer_chat(user_id=user_id, chat_id=chat_id)
    if MaxMessage.objects.filter(
        direction=MaxMessage.Direction.CUSTOMER, external_message_id=mid
    ).exists():
        return DUPLICATE
    if state.chat_id != chat_id:
        state.chat_id = chat_id
        state.save(update_fields=["chat_id", "updated_at"])
    routing = messaging.route_open_request(
        conversations, active_id=state.active_conversation_id
    )
    if routing.closed is not None:
        # The request the customer is writing to has closed. Nothing is stored,
        # nobody is notified, and no other request is chosen for them.
        text, buttons = _closed_reply(routing.closed.request, routing.open)
        queue_message(chat_id=chat_id, text=text, dedupe_key=f"reply:{mid}", buttons=buttons)
        return CLOSED
    if not routing.open:
        queue_message(
            chat_id=chat_id, text=messaging.NO_OPEN_REQUESTS_TEXT, dedupe_key=f"reply:{mid}"
        )
        return CLOSED
    if routing.ambiguous:
        # Never guess: nothing is stored until the customer picks the request.
        queue_message(
            chat_id=chat_id,
            text=AMBIGUOUS_TEXT,
            dedupe_key=f"reply:{mid}",
            buttons=selector_buttons(routing.open),
        )
        return AMBIGUOUS
    active = routing.conversation
    first_customer_message = not MaxMessage.objects.filter(
        conversation=active, direction=MaxMessage.Direction.CUSTOMER
    ).exists()
    now = timezone.now()
    message = MaxMessage.objects.create(
        conversation=active,
        direction=MaxMessage.Direction.CUSTOMER,
        text=(text or "").strip()[:MAX_TEXT_CHARS],
        delivery_status=MaxDeliveryStatus.RECEIVED,
        external_message_id=mid,
    )
    active.last_message_at = now
    active.save(update_fields=["last_message_at", "updated_at"])
    MaxOutboxEvent.objects.create(
        kind=MaxOutboxEvent.Kind.CUSTOMER_MESSAGE,
        request=active.request,
        message=message,
        dedupe_key=f"customer_message:{message.pk}",
        next_attempt_at=now,
    )
    acknowledgement = messaging.acknowledgement_for(
        is_first_customer_message=first_customer_message
    )
    if acknowledgement:
        queue_message(
            chat_id=active.customer_chat_id,
            text=acknowledgement,
            dedupe_key=f"ack:{active.pk}",
            conversation=active,
        )
    return RECORDED


# --- Operator side ---------------------------------------------------------------------


OperatorReplyError = operator_replies.OperatorReplyError


def submit_operator_reply(*, request_id: int, user, text: str, submission_key: str) -> MaxMessage:
    """Queue an employee's reply to a MAX customer; the customer sees the PRO-STOR bot.

    The shared ``operator_replies.submit_reply`` does the work, limited to MAX:
    a request of the other messenger is refused, never answered there.
    """
    return operator_replies.submit_reply(
        request_id=request_id,
        user=user,
        text=text,
        key=submission_key,
        channel=CustomerRequest.Messenger.MAX,
    ).message


def announce_new_requests(*, since, limit: int = 50) -> int:
    """Give MAX requests their conversation and one operator notification.

    The public role writes no MAX table, so the internal worker notices a new
    MAX request instead. ``since`` keeps requests sent before MAX went live
    from being announced all at once.
    """
    if since is None:
        return 0
    requests = list(
        CustomerRequest.objects.filter(
            preferred_messenger=CustomerRequest.Messenger.MAX, created_at__gte=since
        )
        .exclude(max_events__kind=MaxOutboxEvent.Kind.NEW_REQUEST)
        .order_by("pk")[:limit]
    )
    now = timezone.now()
    for request in requests:
        with transaction.atomic():
            ensure_conversation(request)
            MaxOutboxEvent.objects.get_or_create(
                dedupe_key=f"new_request:{request.pk}",
                defaults={
                    "kind": MaxOutboxEvent.Kind.NEW_REQUEST,
                    "request": request,
                    "next_attempt_at": now,
                },
            )
    return len(requests)


def eligible_recipients(*, exclude_user_id=None) -> list:
    """Employees reachable by the operators' notification bot, as DenisStock users."""
    users = [operator.user for operator in telegram_service.active_operators()]
    if exclude_user_id is not None:
        users = [user for user in users if user.pk != exclude_user_id]
    return users


def notification_operator(user):
    """The operator bot identity of a recipient, if they may still be notified."""
    operator = getattr(user, "telegram_operator", None) if user is not None else None
    return operator if telegram_service.operator_is_authorized(operator) else None


def _conversation_for(event: MaxOutboxEvent) -> MaxConversation | None:
    return (
        MaxConversation.objects.select_related("request")
        .prefetch_related("request__lines")
        .filter(request_id=event.request_id)
        .first()
    )


def delivery_content(event: MaxOutboxEvent) -> tuple[str, dict | None]:
    """Text and buttons of one operator notification about MAX, rendered at send time.

    The same notification as Telegram's (``operator_bot``): the employee can
    answer a MAX customer from the bot too. Their own account is never shown
    to the customer.
    """
    conversation = _conversation_for(event)
    request = conversation.request if conversation is not None else event.request
    return operator_bot.notification(event.kind, request, event.message)
