"""Telegram conversations of customer requests: domain rules, no network I/O.

The bot worker (``telegram_bot``) calls these functions inside database
transactions and sends the results afterwards. Everything customer-facing that
must not be lost is stored first (``TelegramMessage``) and delivered from that
row; everything operators must hear about is an ``TelegramOutboxEvent``.
"""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from . import customer_ui, messaging, operator_bot, operator_console, operator_replies, workspace
from .models import (
    CustomerRequest,
    MaxMessage,
    TelegramConversation,
    TelegramCustomerChat,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramOperator,
    TelegramOutboxEvent,
)

MAX_MESSAGE_CHARS = 4000
REPLY_WINDOW = timedelta(minutes=30)
HEX_RE = re.compile(r"^[0-9a-f]{32}$")

LINKED_TEXT = customer_ui.GREETING_TEXT
LINK_INVALID_TEXT = (
    "Ссылка недействительна или устарела. Откройте ссылку со страницы заявки ещё раз. "
    "Если ссылки нет, сервис PRO-STOR свяжется с Вами по телефону."
)
UNLINKED_GREETING = customer_ui.UNLINKED_TEXT
LINKED_GREETING = customer_ui.LINKED_HINT
MEDIA_NOT_SUPPORTED_TEXT = (
    "Пока бот принимает только текст. Опишите деталь словами или отправьте фото в сервис PRO-STOR, "
    "когда получите ответ."
)
CUSTOMER_ACK_TEXT = messaging.CUSTOMER_ACK_TEXT
OPERATOR_HELP_TEXT = operator_console.TELEGRAM_OPERATOR_HELP_TEXT
NOT_AVAILABLE_TEXT = "Недоступно."


def customer_cabinet_enabled() -> bool:
    from .customer_cabinet import cabinet_enabled
    return cabinet_enabled()
# Read by the PostgreSQL insert guard (migration 0006): a role that may only
# INSERT Telegram rows must prove the raw submission key of the target request.
REQUEST_PROOF_SETTING = "denstock.telegram_request_proof"


class TelegramAccessDenied(Exception):
    """The Telegram user is not an authorized operator (anymore)."""


# --- Request creation and linking -------------------------------------------------------


@contextmanager
def request_insert_proof(submission_key: str):
    """Prove ownership of a request to the database for the public role's inserts.

    Use inside the Telegram savepoint: the setting is transaction-local and a
    savepoint rollback reverts it, so it is reset only on success. The raw key
    is the customer's own idempotency key and is never stored.
    """
    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [REQUEST_PROOF_SETTING, submission_key])
    yield
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, '', true)", [REQUEST_PROOF_SETTING])


def start_request_conversation(request: CustomerRequest) -> TelegramConversation:
    """Local rows for a new Telegram-preference request, in its transaction.

    INSERT only (the public role has no other privilege on these tables): the
    conversation that waits for the customer and one operator notification
    whose unique key makes it impossible to notify twice.
    """
    conversation = TelegramConversation.objects.create(request=request)
    TelegramOutboxEvent.objects.create(
        kind=TelegramOutboxEvent.Kind.NEW_REQUEST,
        request=request,
        dedupe_key=f"new_request:{request.pk}",
        next_attempt_at=timezone.now(),
    )
    return conversation


def bind_customer_chat(
    *, request: CustomerRequest, chat_id: int, user_id: int | None, username: str, link_token_id
) -> TelegramConversation:
    """Attach the numeric Telegram chat that consumed a valid one-time link.

    Runs inside ``consume_messenger_start``'s transaction, after the token row
    is locked and marked used, so two consumers cannot both bind.
    """
    now = timezone.now()
    conversation, _created = TelegramConversation.objects.select_for_update().get_or_create(
        request=request
    )
    previous_chat = conversation.customer_chat_id
    conversation.customer_chat_id = chat_id
    conversation.customer_user_id = user_id if isinstance(user_id, int) else None
    conversation.customer_username = str(username or "")[:64]
    conversation.status = TelegramConversation.Status.LINKED
    conversation.linked_at = now
    conversation.save()
    if previous_chat is not None and previous_chat != chat_id:
        TelegramCustomerChat.objects.filter(
            chat_id=previous_chat, active_conversation=conversation
        ).update(active_conversation=None)
    # The request the customer just opened becomes the target of plain messages.
    TelegramCustomerChat.objects.update_or_create(
        chat_id=chat_id, defaults={"active_conversation": conversation}
    )
    TelegramMessage.objects.bulk_create(
        [
            TelegramMessage(
                conversation=conversation,
                direction=TelegramMessage.Direction.SYSTEM,
                text=text,
                delivery_status=TelegramDeliveryStatus.PENDING,
                next_attempt_at=now,
            )
            for text in request_summary_messages(request)
        ]
    )
    # A Telegram identity joins an account only if a MAX-signed-in customer
    # linked it explicitly; this never creates one. Off until enabled.
    from apps.customer_accounts import messenger_hooks as account_hooks

    account_hooks.telegram_handoff(request, user_id=user_id)
    TelegramOutboxEvent.objects.get_or_create(
        dedupe_key=f"customer_linked:{link_token_id}",
        defaults={
            "kind": TelegramOutboxEvent.Kind.CUSTOMER_LINKED,
            "request": request,
            "next_attempt_at": now,
        },
    )
    return conversation


def _summary_policy() -> messaging.SummaryPolicy:
    """Telegram's own limit, read now rather than bound at import.

    The size a transport can carry is the transport's business, and a test
    that narrows it must be able to narrow it here.
    """
    return messaging.SummaryPolicy(linked_text=LINKED_TEXT, message_limit=MAX_MESSAGE_CHARS)


def request_summary_messages(request: CustomerRequest) -> list[str]:
    """The shared order summary, sized for Telegram."""
    return messaging.request_summary_messages(request, _summary_policy())


def anonymize_conversation(request: CustomerRequest) -> None:
    """Privacy minimization mirrors the request: texts and identities are erased."""
    conversation = TelegramConversation.objects.filter(request=request).first()
    if conversation is None:
        return
    TelegramMessage.objects.filter(conversation=conversation).update(text="")
    TelegramCustomerChat.objects.filter(active_conversation=conversation).update(
        active_conversation=None
    )
    chat_id = conversation.customer_chat_id
    if chat_id is not None and not (
        TelegramConversation.objects.filter(
            customer_chat_id=chat_id, status=TelegramConversation.Status.LINKED
        )
        .exclude(pk=conversation.pk)
        .exists()
    ):
        # The routing row is keyed by the raw chat id: keep no identity for a
        # customer whose only remaining link is the anonymized request.
        TelegramCustomerChat.objects.filter(chat_id=chat_id).delete()
    conversation.customer_chat_id = None
    conversation.customer_user_id = None
    conversation.customer_username = ""
    conversation.status = TelegramConversation.Status.CLOSED
    conversation.save()


def customer_contact_allowed(request: CustomerRequest) -> bool:
    return messaging.customer_contact_allowed(request)


# --- Operators ---------------------------------------------------------------------------


def operator_is_authorized(operator: TelegramOperator | None) -> bool:
    return bool(
        operator is not None
        and operator.is_active
        and operator.user.is_active
        and operator.user.can_manage_sales
    )


def authorized_operator(telegram_user_id, *, lock: bool = False) -> TelegramOperator | None:
    """Re-read authorization for every update and every button press."""
    if not isinstance(telegram_user_id, int) or isinstance(telegram_user_id, bool):
        return None
    if telegram_user_id <= 0:
        return None
    queryset = TelegramOperator.objects.select_related("user").filter(
        telegram_user_id=telegram_user_id
    )
    if lock:
        queryset = queryset.select_for_update()
    operator = queryset.first()
    return operator if operator_is_authorized(operator) else None


def active_operators(*, exclude_id=None) -> list[TelegramOperator]:
    queryset = TelegramOperator.objects.select_related("user").filter(
        is_active=True, user__is_active=True
    )
    if exclude_id is not None:
        queryset = queryset.exclude(pk=exclude_id)
    return [operator for operator in queryset if operator.user.can_manage_sales]


def operator_display_name(user) -> str:
    return operator_bot.display_name(user)


# --- Cards -------------------------------------------------------------------------------


def _conversation_queryset():
    return TelegramConversation.objects.select_related("request").prefetch_related(
        "request__lines"
    )


def conversation_by_hex(value: str) -> TelegramConversation | None:
    if not isinstance(value, str) or not HEX_RE.fullmatch(value):
        return None
    return _conversation_queryset().filter(public_id=uuid.UUID(hex=value)).first()


def _target_of(request_or_conversation):
    """The reply target, without re-reading a conversation the caller already has."""
    request = getattr(request_or_conversation, "request", None)
    if request is None:
        return request_or_conversation, None
    return request, operator_replies.ReplyTarget(
        request=request,
        channel=request.preferred_messenger,
        conversation=request_or_conversation,
    )


def request_card_text(request_or_conversation, *, heading: str = "ЗАЯВКА") -> str:
    """Operator card of a request (or of the request of a conversation)."""
    request, target = _target_of(request_or_conversation)
    return operator_bot.card_text(request, heading=heading, target=target)


def internal_request_url(request: CustomerRequest) -> str | None:
    return operator_bot.internal_request_url(request)


def operator_buttons(request_or_conversation) -> dict:
    request, target = _target_of(request_or_conversation)
    return operator_bot.request_buttons(request, target=target)


def operator_menu() -> tuple[str, dict]:
    return (
        operator_console.TELEGRAM_OPERATOR_PANEL_TITLE,
        operator_console.telegram_operator_keyboard(),
    )


def operator_start_menu() -> tuple[str, dict]:
    return OPERATOR_HELP_TEXT, operator_console.telegram_operator_keyboard()


def operator_request_page(page: int, *, new_only: bool = False) -> tuple[str, dict | None]:
    return operator_bot.request_page(page, new_only=new_only)


def delivery_content(event: TelegramOutboxEvent) -> tuple[str, dict | None]:
    """Text and buttons of one operator notification, rendered at send time."""
    conversation = _conversation_queryset().filter(request_id=event.request_id).first()
    request = conversation.request if conversation is not None else event.request
    return operator_bot.notification(event.kind, request, event.message)


# --- Operator actions --------------------------------------------------------------------
#
# Reply mode is the one piece of state behind the operators' bot: the request an
# employee's next plain text goes to. Only that employee's own press of
# [Ответить] sets it. Notifications, lists and cards never touch it, so a
# message about request B arriving while the employee answers A changes nothing.


def _clear_reply_mode(operator: TelegramOperator) -> None:
    operator.reply_request = None
    operator.reply_conversation = None
    operator.reply_started_at = None
    operator.save(
        update_fields=["reply_request", "reply_conversation", "reply_started_at", "updated_at"]
    )


@transaction.atomic
def begin_reply(*, telegram_user_id: int, conversation_hex: str) -> tuple[str, dict | None]:
    """Enter reply mode for the request a button names, if it can be answered now.

    ``conversation_hex`` is the hex a button carries: a request's own id, or a
    Telegram conversation's id on a button sent before this release.
    """
    operator = authorized_operator(telegram_user_id, lock=True)
    if operator is None:
        raise TelegramAccessDenied
    request = operator_bot.request_by_hex(conversation_hex)
    if request is None:
        return "Заявка не найдена.", operator_bot.back_to_list()
    target = operator_replies.reply_target(request)
    reason = target.blocked_reason()
    if reason:
        # Server state decides, not the age of the button. An operator already
        # answering another request keeps that target.
        return reason, operator_bot.request_buttons(request, target=target)
    operator.reply_request = request
    operator.reply_conversation = None
    operator.reply_started_at = timezone.now()
    operator.save(
        update_fields=["reply_request", "reply_conversation", "reply_started_at", "updated_at"]
    )
    return operator_bot.reply_prompt(
        request, target=target, last_message=workspace.latest_customer_message(request)
    )


@transaction.atomic
def cancel_reply(*, telegram_user_id: int) -> str:
    operator = authorized_operator(telegram_user_id, lock=True)
    if operator is None:
        raise TelegramAccessDenied
    _clear_reply_mode(operator)
    return "Ответ отменён."


def _stored_bot_reply(update_id: int) -> bool:
    return (
        TelegramMessage.objects.filter(telegram_update_id=update_id).exists()
        or MaxMessage.objects.filter(dedupe_key=f"operator_reply:tg:{update_id}").exists()
    )


def submit_operator_reply(
    *, telegram_user_id: int, update_id: int, text: str
) -> str | tuple[str, dict | None]:
    """Queue the operator's text for the request in reply mode; the worker delivers it.

    Returns the confirmation, with buttons when there is somewhere to go next.
    Reply mode ends with this message whatever happens: the next text needs a
    new, explicit [Ответить].
    """
    with transaction.atomic():
        if _stored_bot_reply(update_id):
            return ""
        operator = authorized_operator(telegram_user_id, lock=True)
        if operator is None:
            raise TelegramAccessDenied
        request = operator.reply_request
        started = operator.reply_started_at
        if request is None or started is None:
            return (
                "Чтобы ответить клиенту, откройте заявку и нажмите «Ответить». "
                + OPERATOR_HELP_TEXT,
                operator_bot.back_to_list(),
            )
        _clear_reply_mode(operator)
        if timezone.now() - started > REPLY_WINDOW:
            return "Режим ответа истёк. Нажмите «Ответить» ещё раз.", operator_bot.back_to_list()
    try:
        result = operator_replies.submit_reply(
            request_id=request.pk,
            user=operator.user,
            text=text,
            key=f"tg:{update_id}",
            telegram_operator=operator,
            telegram_update_id=update_id,
        )
    except operator_replies.OperatorReplyError as exc:
        return str(exc), operator_bot.back_to_list()
    return (
        f"Ответ по заявке №{request.reference} поставлен в отправку клиенту в {result.label}.",
        operator_bot.after_reply_buttons(request),
    )


# --- Customer actions --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CustomerResult:
    reply: str = ""
    keyboard: dict | None = None


def _linked_conversations(chat_id: int) -> list[TelegramConversation]:
    return list(
        TelegramConversation.objects.select_related("request")
        .prefetch_related("request__lines")
        .filter(customer_chat_id=chat_id, status=TelegramConversation.Status.LINKED)
        .order_by("-linked_at", "-pk")[:20]
    )


# The customer's one control, always under the text field. It is not a command
# and does not get in the way of typing an ordinary message.
def customer_keyboard() -> dict:
    return {
        "keyboard": [[{"text": customer_ui.MY_REQUESTS_BUTTON}]],
        "resize_keyboard": True,
        "is_persistent": False,
        "one_time_keyboard": False,
    }


# Backward-compatible import for adapters/tests; callers that render a menu
# use the function so runtime flag changes are respected.
MY_REQUESTS_TEXTS = {
    customer_ui.MY_REQUESTS_BUTTON.lower(),
    "мои заявки",
    "заявки",
    "/requests",
}
MY_PURCHASES_TEXTS = {customer_ui.MY_PURCHASES_BUTTON.lower(), "покупки"}


def _purchase_buttons(purchases) -> dict | None:
    if not purchases:
        return None
    return {
        "inline_keyboard": [
            [{"text": f"Открыть №{purchase.number}",
              "callback_data": f"{customer_ui.PURCHASE_PAYLOAD_PREFIX}{purchase.sale_id}"}]
            for purchase in purchases
        ]
    }


def purchase_selector_result(
    chat_id: int, *, provider_user_id: int | None = None
) -> CustomerResult:
    from .customer_cabinet import cabinet_enabled, list_customer_purchases

    if not cabinet_enabled():
        return CustomerResult(NOT_AVAILABLE_TEXT, customer_keyboard())

    purchases = list_customer_purchases(
        provider="telegram", provider_user_id=provider_user_id or chat_id
    )
    if not purchases:
        return CustomerResult("История покупок пока недоступна.", customer_keyboard())
    text = "Мои покупки:\n\n" + "\n\n".join(
        customer_ui.purchase_summary_text(purchase) for purchase in purchases
    )
    return CustomerResult(text, _purchase_buttons(purchases) or customer_keyboard())


def purchase_detail_result(
    *, chat_id: int, sale_id: str, provider_user_id: int | None = None
) -> CustomerResult:
    from .customer_cabinet import cabinet_enabled, get_customer_purchase
    if not cabinet_enabled():
        return CustomerResult(NOT_AVAILABLE_TEXT, customer_keyboard())

    try:
        sale_id_int = int(sale_id)
    except (TypeError, ValueError):
        sale_id_int = -1
    purchase = get_customer_purchase(
        provider="telegram", provider_user_id=provider_user_id or chat_id, sale_id=sale_id_int
    )
    if purchase is None:
        return CustomerResult("Покупка недоступна.", customer_keyboard())
    return CustomerResult(
        customer_ui.purchase_detail_text(purchase),
        {"inline_keyboard": [[
            {"text": "Повторить покупку",
             "callback_data": f"{customer_ui.REORDER_PAYLOAD_PREFIX}{purchase.sale_id}"},
             {"text": "Назад", "callback_data": customer_ui.MY_PURCHASES_PAYLOAD},
        ]]},
    )


def reorder_preview_result(
    *, chat_id: int, sale_id: str, provider_user_id: int | None = None
) -> CustomerResult:
    from .customer_cabinet import build_reorder_preview, cabinet_enabled
    if not cabinet_enabled():
        return CustomerResult(NOT_AVAILABLE_TEXT, customer_keyboard())

    try:
        sale_id_int = int(sale_id)
    except (TypeError, ValueError):
        sale_id_int = -1
    preview = build_reorder_preview(
        provider="telegram", provider_user_id=provider_user_id or chat_id, sale_id=sale_id_int
    )
    if preview is None:
        return CustomerResult("Покупка недоступна.", customer_keyboard())
    buttons = [[
        {"text": "Создать заявку",
         "callback_data": f"{customer_ui.REORDER_CONFIRM_PAYLOAD_PREFIX}{sale_id_int}"},
        {"text": "Отмена", "callback_data": customer_ui.MY_PURCHASES_PAYLOAD},
    ]]
    return CustomerResult(customer_ui.reorder_preview_text(preview), {"inline_keyboard": buttons})


def confirm_reorder_result(*, chat_id: int, sale_id: str, callback_key: str,
                           provider_user_id: int | None = None) -> CustomerResult:
    from .customer_cabinet import (
        CabinetAccessError,
        cabinet_enabled,
        create_request_from_reorder_preview,
    )
    from .services import CustomerRequestError
    if not cabinet_enabled():
        return CustomerResult(NOT_AVAILABLE_TEXT, customer_keyboard())

    try:
        sale_id_int = int(sale_id)
    except (TypeError, ValueError):
        sale_id_int = -1
    try:
        request, created = create_request_from_reorder_preview(
            provider="telegram",
            provider_user_id=provider_user_id or chat_id,
            sale_id=sale_id_int,
            submission_key=f"messenger-reorder-tg-{chat_id}-{callback_key}",
        )
    except (CabinetAccessError, CustomerRequestError) as exc:
        return CustomerResult(str(exc), customer_keyboard())
    suffix = "создана" if created else "уже создана"
    return CustomerResult(f"Заявка №{request.reference} {suffix}.", customer_keyboard())


def _selector(conversations, *, current_id=None) -> dict:
    """One inline button per active request; the current one is marked."""
    view = customer_ui.SelectorView(text="", conversations=list(conversations),
                                    current_id=current_id)
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": payload}] for label, payload in view.buttons()
        ]
    }


def selector_result(chat_id: int) -> CustomerResult:
    """«Мои заявки»: what the customer has open now, and which one is chosen."""
    linked = _linked_conversations(chat_id)
    state = TelegramCustomerChat.objects.filter(chat_id=chat_id).first()
    view = customer_ui.selector_view(
        linked,
        active_id=state.active_conversation_id if state else None,
        linked_any=bool(linked),
    )
    if not view.has_choices:
        return CustomerResult(view.text, customer_keyboard())
    return CustomerResult(view.text, _selector(view.conversations, current_id=view.current_id))


def selector_text_and_markup(chat_id: int) -> tuple[str, dict | None]:
    """The selector as it should look now, for re-rendering an existing message."""
    result = selector_result(chat_id)
    keyboard = result.keyboard if result.keyboard != customer_keyboard() else None
    return result.reply, keyboard


CONTACT_CLOSED_TEXT = "Переписка по этой заявке закрыта."


def _closed_reply(request: CustomerRequest, still_open) -> CustomerResult:
    """What to say about a request the customer can no longer write about.

    A closed status gets the closed-request text and the requests still open.
    Withdrawn consent or anonymization leaves the request itself open, so it
    keeps its own wording and offers nothing to switch to.
    """
    if request.status in messaging.MESSAGEABLE_STATUSES:
        return CustomerResult(CONTACT_CLOSED_TEXT)
    text = messaging.closed_request_text(request.reference, other_open=bool(still_open))
    return CustomerResult(text, _selector(still_open) if still_open else customer_keyboard())


def customer_greeting(chat_id: int) -> CustomerResult:
    """A plain hello: the customer's control comes with it, not a command list."""
    linked = _linked_conversations(chat_id)
    if messaging.open_conversations(linked):
        return CustomerResult(LINKED_GREETING, customer_keyboard())
    text = messaging.NO_OPEN_REQUESTS_TEXT if linked else UNLINKED_GREETING
    return CustomerResult(text, customer_keyboard() if linked else None)


def customer_conversations_prompt(chat_id: int) -> CustomerResult:
    return selector_result(chat_id)


@transaction.atomic
def select_customer_conversation(*, chat_id: int, conversation_hex: str):
    """Only a conversation already bound to this very chat can be selected."""
    if not isinstance(chat_id, int) or not HEX_RE.fullmatch(str(conversation_hex or "")):
        return None
    conversation = (
        TelegramConversation.objects.select_related("request")
        .filter(
            public_id=uuid.UUID(hex=conversation_hex),
            customer_chat_id=chat_id,
            status=TelegramConversation.Status.LINKED,
        )
        .first()
    )
    if conversation is None or not messaging.customer_can_message(conversation.request):
        # A closed request is never selected; the current choice is left as is.
        return None
    TelegramCustomerChat.objects.update_or_create(
        chat_id=chat_id, defaults={"active_conversation": conversation}
    )
    return conversation


def closed_selection(*, chat_id: int, conversation_hex: str) -> CustomerResult | None:
    """The answer to an old button of this chat's own request that has closed.

    ``None`` means the button was not this chat's request at all, which the bot
    refuses without saying anything about whose it might be.
    """
    if not isinstance(chat_id, int) or not HEX_RE.fullmatch(str(conversation_hex or "")):
        return None
    conversation = (
        TelegramConversation.objects.select_related("request")
        .filter(
            public_id=uuid.UUID(hex=conversation_hex),
            customer_chat_id=chat_id,
            status=TelegramConversation.Status.LINKED,
        )
        .first()
    )
    if conversation is None or messaging.customer_can_message(conversation.request):
        return None
    still_open = messaging.open_conversations(_linked_conversations(chat_id))
    return _closed_reply(conversation.request, still_open)


@transaction.atomic
def record_customer_message(*, chat_id: int, update_id: int, text: str) -> CustomerResult:
    if TelegramMessage.objects.filter(telegram_update_id=update_id).exists():
        return CustomerResult()
    conversations = _linked_conversations(chat_id)
    if not conversations:
        return CustomerResult(UNLINKED_GREETING)
    state = TelegramCustomerChat.objects.select_for_update().filter(chat_id=chat_id).first()
    routing = messaging.route_open_request(
        conversations, active_id=state.active_conversation_id if state else None
    )
    if routing.closed is not None:
        # The request the customer is writing to has closed. Nothing is stored,
        # nobody is notified, and no other request is chosen for them.
        return _closed_reply(routing.closed.request, routing.open)
    if not routing.open:
        return CustomerResult(messaging.NO_OPEN_REQUESTS_TEXT)
    if routing.ambiguous:
        # Never guess: an ambiguous message is not stored until the
        # customer says which request it belongs to.
        return CustomerResult(
            "У вас несколько заявок. Выберите нужную и отправьте сообщение ещё раз.",
            _selector(routing.open),
        )
    active = routing.conversation
    # ``state`` is locked above.  All normal messages for this chat therefore
    # serialize here, and the persisted first message is the durable ACK marker.
    # A worker restart, outbox retry, or replay cannot turn a later message into
    # a first one.
    first_customer_message = not TelegramMessage.objects.filter(
        conversation=active, direction=TelegramMessage.Direction.CUSTOMER
    ).exists()
    now = timezone.now()
    message = TelegramMessage.objects.create(
        conversation=active,
        direction=TelegramMessage.Direction.CUSTOMER,
        text=(text or "").strip()[:MAX_MESSAGE_CHARS],
        delivery_status=TelegramDeliveryStatus.RECEIVED,
        telegram_update_id=update_id,
    )
    active.last_message_at = now
    active.save(update_fields=["last_message_at", "updated_at"])
    TelegramOutboxEvent.objects.create(
        kind=TelegramOutboxEvent.Kind.CUSTOMER_MESSAGE,
        request=active.request,
        message=message,
        dedupe_key=f"customer_message:{message.pk}",
        next_attempt_at=now,
    )
    return CustomerResult(
        messaging.acknowledgement_for(is_first_customer_message=first_customer_message)
    )
