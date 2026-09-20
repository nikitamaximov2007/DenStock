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

from . import customer_ui, messaging, operator_bot, operator_replies, telegram_service
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

LINKED_TEXT = customer_ui.GREETING_TEXT
LINK_INVALID_TEXT = (
    "Ссылка недействительна или устарела. Откройте ссылку со страницы заявки ещё раз. "
    "Если ссылки нет, менеджер свяжется с вами по телефону."
)
UNLINKED_GREETING = customer_ui.UNLINKED_TEXT
LINKED_GREETING = customer_ui.LINKED_HINT
MEDIA_NOT_SUPPORTED_TEXT = telegram_service.MEDIA_NOT_SUPPORTED_TEXT
SELECT_TEXT = customer_ui.SELECTOR_HINT
AMBIGUOUS_TEXT = "У вас несколько заявок. Выберите нужную и отправьте сообщение ещё раз."
SELECTED_TEXT = customer_ui.SELECTED_TEXT
# MAX has no persistent keyboard, so the bot's own messages carry the entry point.
MENU_PAYLOAD = customer_ui.MY_REQUESTS_PAYLOAD
MENU_BUTTON = [[
    {"text": customer_ui.MY_REQUESTS_BUTTON, "payload": MENU_PAYLOAD},
    {"text": customer_ui.MY_PURCHASES_BUTTON, "payload": customer_ui.MY_PURCHASES_PAYLOAD},
]]
# A selector answer re-renders the pressed message instead of sending a new one.
SELECTOR_DEDUPE_PREFIX = "selector:"
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
    in_place: bool = False,
) -> tuple[MaxMessage, bool]:
    """Store one outgoing message exactly once per ``dedupe_key``.

    ``in_place`` marks a selector the worker should render by editing the
    message the customer pressed, instead of adding one to the conversation.
    The mark rides on the dedupe key, so no column is needed and a worker that
    cannot edit still delivers the same text as an ordinary message.
    """
    if in_place:
        dedupe_key = f"{SELECTOR_DEDUPE_PREFIX}{dedupe_key}"
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
    # MAX has no persistent keyboard, so the way in rides on the greeting itself:
    # the last summary message carries «Мои заявки». Same deduplicated rows, so a
    # redelivered handoff still greets exactly once.
    summary = request_summary_messages(request)
    for index, text in enumerate(summary):
        queue_message(
            chat_id=chat_id,
            text=text,
            dedupe_key=f"summary:{link_token_id}:{index}",
            conversation=conversation,
            buttons=MENU_BUTTON if index == len(summary) - 1 else None,
        )
    # The verified MAX identity's PRO-STOR account owns this request as well
    # (created on first sight). Off until the account feature is enabled.
    from apps.customer_accounts import messenger_hooks as account_hooks

    account_hooks.max_handoff(request, user_id=user_id)
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
        .prefetch_related("request__lines")
        .filter(customer_user_id=user_id, status=MaxConversation.Status.LINKED)
        .order_by("-linked_at", "-pk")[:SELECTOR_LIMIT]
    )


def selector_buttons(conversations, *, current_id=None) -> list[list[dict]]:
    """One button per request; the current one is marked, the payload is opaque."""
    view = customer_ui.SelectorView(
        text="", conversations=list(conversations), current_id=current_id
    )
    return [[{"text": label, "payload": payload}] for label, payload in view.buttons()]


def selector_view(user_id: int) -> tuple[customer_ui.SelectorView, list[list[dict]]]:
    """«Мои заявки» for this MAX user: the text and the buttons to draw."""
    linked = linked_conversations(user_id)
    state = MaxCustomerChat.objects.filter(user_id=user_id).first()
    view = customer_ui.selector_view(
        linked,
        active_id=state.active_conversation_id if state else None,
        linked_any=bool(linked),
    )
    buttons = selector_buttons(view.conversations, current_id=view.current_id) or None
    return view, buttons


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
    """A hello with the way in: «Мои заявки», never a command to memorise."""
    linked = linked_conversations(user_id)
    open_requests = messaging.open_conversations(linked)
    if open_requests:
        text, buttons = LINKED_GREETING, MENU_BUTTON
    elif linked:
        text, buttons = messaging.NO_OPEN_REQUESTS_TEXT, None
    else:
        text, buttons = UNLINKED_GREETING, None
    queue_message(chat_id=chat_id, text=text, dedupe_key=dedupe_key, buttons=buttons)


def queue_selector(*, user_id: int, chat_id: int, dedupe_key: str, callback_id: str = "") -> None:
    """«Мои заявки»: the customer's active requests with the current one marked.

    A closed request is never offered, whatever an older keyboard still shows.
    With ``callback_id`` the answer re-renders the pressed message in place.
    """
    view, buttons = selector_view(user_id)
    queue_message(
        chat_id=chat_id,
        text=view.text,
        dedupe_key=dedupe_key,
        buttons=buttons,
        callback_id=callback_id,
        in_place=bool(callback_id and buttons),
    )


def is_menu_payload(payload) -> bool:
    return isinstance(payload, str) and payload.strip() == MENU_PAYLOAD


def is_purchases_payload(payload) -> bool:
    return isinstance(payload, str) and payload.strip() == customer_ui.MY_PURCHASES_PAYLOAD


def _purchase_buttons(purchases) -> list[list[dict]] | None:
    if not purchases:
        return None
    return [
        [{"text": f"Открыть №{purchase.number}",
          "payload": f"{customer_ui.PURCHASE_PAYLOAD_PREFIX}{purchase.sale_id}"}]
        for purchase in purchases
    ]


def purchase_selector_view(user_id: int) -> tuple[str, list[list[dict]] | None]:
    from .customer_cabinet import list_customer_purchases

    purchases = list_customer_purchases(provider="max", provider_user_id=user_id)
    if not purchases:
        return "История покупок пока недоступна.", MENU_BUTTON
    text = "Мои покупки:\n\n" + "\n\n".join(
        customer_ui.purchase_summary_text(purchase) for purchase in purchases
    )
    return text, _purchase_buttons(purchases) or MENU_BUTTON


def purchase_detail_view(*, user_id: int, sale_id: str) -> tuple[str, list[list[dict]]]:
    from .customer_cabinet import get_customer_purchase

    try:
        sale_id_int = int(sale_id)
    except (TypeError, ValueError):
        sale_id_int = -1
    purchase = get_customer_purchase(
        provider="max", provider_user_id=user_id, sale_id=sale_id_int
    )
    if purchase is None:
        return "Покупка недоступна.", MENU_BUTTON
    return customer_ui.purchase_detail_text(purchase), [[
        {"text": "Повторить покупку",
         "payload": f"{customer_ui.REORDER_PAYLOAD_PREFIX}{purchase.sale_id}"},
        {"text": "Назад", "payload": customer_ui.MY_PURCHASES_PAYLOAD},
    ]]


def reorder_preview_view(*, user_id: int, sale_id: str) -> tuple[str, list[list[dict]]]:
    from .customer_cabinet import build_reorder_preview

    try:
        sale_id_int = int(sale_id)
    except (TypeError, ValueError):
        sale_id_int = -1
    preview = build_reorder_preview(provider="max", provider_user_id=user_id, sale_id=sale_id_int)
    if preview is None:
        return "Покупка недоступна.", MENU_BUTTON
    return customer_ui.reorder_preview_text(preview), [[
        {"text": "Создать заявку",
         "payload": f"{customer_ui.REORDER_CONFIRM_PAYLOAD_PREFIX}{sale_id_int}"},
        {"text": "Отмена", "payload": customer_ui.MY_PURCHASES_PAYLOAD},
    ]]


def confirm_reorder_view(
    *, user_id: int, sale_id: str, callback_key: str
) -> tuple[str, list[list[dict]]]:
    from .customer_cabinet import CabinetAccessError, create_request_from_reorder_preview

    try:
        sale_id_int = int(sale_id)
    except (TypeError, ValueError):
        sale_id_int = -1
    try:
        request, created = create_request_from_reorder_preview(
            provider="max",
            provider_user_id=user_id,
            sale_id=sale_id_int,
            submission_key=f"messenger-reorder-max-{user_id}-{callback_key}",
        )
    except CabinetAccessError as exc:
        return str(exc), MENU_BUTTON
    suffix = "создана" if created else "уже создана"
    return f"Заявка №{request.reference} {suffix}.", MENU_BUTTON


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
    if MaxMessage.objects.filter(
        dedupe_key__in=[dedupe_key, f"{SELECTOR_DEDUPE_PREFIX}{dedupe_key}"]
    ).exists():
        return conversation  # a redelivered press: already selected and confirmed
    # The choice is written first; drawing it is a separate, losable step.
    MaxCustomerChat.objects.update_or_create(
        user_id=user_id, defaults={"chat_id": chat_id, "active_conversation": conversation}
    )
    # The pressed selector is re-drawn in place, so the ✓ moves without adding
    # anything, and the customer gets one short line saying what is chosen now.
    queue_selector(
        user_id=user_id, chat_id=chat_id, dedupe_key=dedupe_key, callback_id=callback_id
    )
    queue_message(
        chat_id=chat_id,
        text=customer_ui.selected_text(conversation.request.reference),
        dedupe_key=dedupe_key,
        conversation=conversation,
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
