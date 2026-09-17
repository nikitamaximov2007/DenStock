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
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.core.templatetags.number_format import money_int, quantity_int

from . import messaging
from .models import (
    CustomerRequest,
    TelegramConversation,
    TelegramCustomerChat,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramOperator,
    TelegramOutboxEvent,
)

MAX_MESSAGE_CHARS = 4000
CARD_TEXT_LIMIT = 3600
LIST_PAGE_SIZE = 8
REPLY_WINDOW = timedelta(minutes=30)
HEX_RE = re.compile(r"^[0-9a-f]{32}$")

LINKED_TEXT = "Готово. Telegram подключён к заявке {reference}."
LINK_INVALID_TEXT = (
    "Ссылка недействительна или устарела. Откройте ссылку со страницы заявки ещё раз. "
    "Если ссылки нет, менеджер свяжется с вами по телефону."
)
UNLINKED_GREETING = (
    "Это бот PRO-STOR для связи по заявкам. Чтобы написать менеджеру, откройте ссылку "
    "«Продолжить в Telegram» со страницы вашей заявки."
)
LINKED_GREETING = (
    "Напишите сообщение, менеджер PRO-STOR ответит здесь. Если у вас несколько заявок, "
    "команда /requests поможет выбрать нужную."
)
MEDIA_NOT_SUPPORTED_TEXT = (
    "Пока бот принимает только текст. Опишите деталь словами или отправьте фото менеджеру, "
    "когда он ответит."
)
CUSTOMER_ACK_TEXT = messaging.CUSTOMER_ACK_TEXT
OPERATOR_HELP_TEXT = (
    "Команды: /requests: открытые заявки с Telegram, /cancel: отменить ответ, "
    "/whoami: ваш Telegram ID."
)
NOT_AVAILABLE_TEXT = "Недоступно."
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
    if user is None:
        return "сотрудник"
    full_name = getattr(user, "full_name", "") or user.get_full_name()
    return full_name or user.get_username()


# --- Cards -------------------------------------------------------------------------------


def _link_state(conversation, channel: str = "Telegram") -> str:
    return f"{channel} подключён" if conversation.is_linked else "ожидает подключения"


def _conversation_queryset():
    return TelegramConversation.objects.select_related("request").prefetch_related(
        "request__lines"
    )


def conversation_by_hex(value: str) -> TelegramConversation | None:
    if not isinstance(value, str) or not HEX_RE.fullmatch(value):
        return None
    return _conversation_queryset().filter(public_id=uuid.UUID(hex=value)).first()


def request_card_text(
    conversation, *, heading: str = "ЗАЯВКА", channel: str = "Telegram"
) -> str:
    """Operator card built only from the stored request and its line snapshots.

    ``conversation`` is any transport conversation with ``request`` and
    ``is_linked``; ``channel`` names that transport on the card.
    """
    request = conversation.request
    lines = list(request.lines.all())
    out = [f"{heading} {request.reference}", f"Статус: {request.get_status_display()}"]
    if request.data_anonymized_at:
        out.append("Клиент: данные обезличены")
    else:
        out.append(f"Клиент: {request.customer_name}")
        out.append(f"Телефон: {request.customer_phone}")
    out.append(f"Связь: {channel}, {_link_state(conversation, channel)}")
    if request.consent_withdrawn_at:
        out.append("Клиент отозвал согласие на связь")
    out.append("Позиции:")
    total = Decimal("0")
    priced = False
    rendered = []
    for number, line in enumerate(lines, start=1):
        quantity = f"{quantity_int(line.quantity_requested)} {line.unit_short_name}".strip()
        head = f"{number}. {line.article or 'без артикула'} · {line.part_name}"
        if line.is_supply_inquiry:
            head += " · запрос о поставке"
        if line.price_seen is None:
            detail = f"   {quantity} · цена уточняется"
        else:
            detail = f"   {quantity} × {money_int(line.price_seen)} ₽"
            if not line.is_supply_inquiry:
                line_total = line.price_seen * line.quantity_requested
                total += line_total
                priced = True
                detail += f" = {money_int(line_total)} ₽"
        rendered.append(f"{head}\n{detail}")
    used = sum(len(part) + 1 for part in out)
    for index, item in enumerate(rendered):
        if used + len(item) > CARD_TEXT_LIMIT:
            out.append(f"… и ещё позиций: {len(rendered) - index}. Полностью в DenisStock.")
            break
        out.append(item)
        used += len(item) + 1
    if priced:
        out.append(f"Сумма по деталям в наличии (цены на момент заявки): {money_int(total)} ₽")
    if request.comment and not request.data_anonymized_at:
        out.append(f"Комментарий: {request.comment[:500]}")
    return "\n".join(out)


def internal_request_url(request: CustomerRequest) -> str | None:
    base = settings.TELEGRAM_INTERNAL_BASE_URL
    if not base.startswith(("https://", "http://")):
        return None
    return f"{base}{reverse('customer_request_detail', args=[request.pk])}"


def operator_buttons(conversation: TelegramConversation) -> dict:
    token = conversation.public_id.hex
    first_row = [{"text": "Ответить", "callback_data": f"r:{token}"}]
    url = internal_request_url(conversation.request)
    if url:
        first_row.append({"text": "Открыть заявку", "url": url})
    return {
        "inline_keyboard": [
            first_row,
            [{"text": "Обновить", "callback_data": f"c:{token}"}],
        ]
    }


def operator_menu() -> tuple[str, dict]:
    return (
        "Бот заявок PRO-STOR. Здесь приходят новые заявки и сообщения клиентов.",
        {"inline_keyboard": [[{"text": "Открытые заявки", "callback_data": "l:1"}]]},
    )


def operator_request_page(page: int) -> tuple[str, dict | None]:
    queryset = (
        TelegramConversation.objects.select_related("request")
        .filter(
            request__status__in=[
                CustomerRequest.Status.NEW,
                CustomerRequest.Status.IN_PROGRESS,
            ]
        )
        .order_by("-request__created_at", "-pk")
    )
    total = queryset.count()
    if not total:
        return "Открытых заявок с Telegram нет.", None
    pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
    page = min(max(1, page), pages)
    start = (page - 1) * LIST_PAGE_SIZE
    rows = []
    for conversation in queryset[start : start + LIST_PAGE_SIZE]:
        request = conversation.request
        name = "данные обезличены" if request.data_anonymized_at else request.customer_name
        state = "подключён" if conversation.is_linked else "ожидает"
        rows.append(
            [
                {
                    "text": f"{request.reference} · {name[:24]} · {state}",
                    "callback_data": f"c:{conversation.public_id.hex}",
                }
            ]
        )
    navigation = []
    if page > 1:
        navigation.append({"text": "Назад", "callback_data": f"l:{page - 1}"})
    if page < pages:
        navigation.append({"text": "Дальше", "callback_data": f"l:{page + 1}"})
    if navigation:
        rows.append(navigation)
    return f"Открытые заявки с Telegram, страница {page} из {pages}:", {"inline_keyboard": rows}


def delivery_content(event: TelegramOutboxEvent) -> tuple[str, dict | None]:
    """Text and buttons of one operator notification, rendered at send time."""
    conversation = (
        _conversation_queryset().filter(request_id=event.request_id).first()
    )
    if conversation is None:
        return f"Заявка {event.request.reference}: переписка недоступна.", None
    reference = conversation.request.reference
    buttons = operator_buttons(conversation)
    if event.kind == TelegramOutboxEvent.Kind.NEW_REQUEST:
        return request_card_text(conversation, heading="НОВАЯ ЗАЯВКА"), buttons
    if event.kind == TelegramOutboxEvent.Kind.CUSTOMER_LINKED:
        return f"Клиент подключил Telegram к заявке {reference}.", buttons
    message = event.message
    text = message.text if message else ""
    if event.kind == TelegramOutboxEvent.Kind.CUSTOMER_MESSAGE:
        return f"Заявка {reference} · сообщение клиента:\n{text}", buttons
    author = operator_display_name(message.operator_user if message else None)
    return f"Заявка {reference} · ответ клиенту отправлен.\nСотрудник: {author}\n{text}", buttons


# --- Operator actions --------------------------------------------------------------------


@transaction.atomic
def begin_reply(*, telegram_user_id: int, conversation_hex: str) -> tuple[str, dict | None]:
    operator = authorized_operator(telegram_user_id, lock=True)
    if operator is None:
        raise TelegramAccessDenied
    conversation = conversation_by_hex(conversation_hex)
    if conversation is None:
        return "Заявка не найдена.", None
    reference = conversation.request.reference
    if not customer_contact_allowed(conversation.request):
        return f"По заявке {reference} клиент отозвал согласие на связь.", None
    if not conversation.is_linked:
        return (
            f"Клиент ещё не подключил Telegram к заявке {reference}. "
            "Ответить через бота пока нельзя, свяжитесь по телефону.",
            None,
        )
    operator.reply_conversation = conversation
    operator.reply_started_at = timezone.now()
    operator.save(update_fields=["reply_conversation", "reply_started_at", "updated_at"])
    return (
        f"Ответ на заявку {reference}\n"
        "Напишите сообщение одним текстом. Клиент увидит его от имени бота PRO-STOR.",
        {"inline_keyboard": [[{"text": "Отмена", "callback_data": "x"}]]},
    )


@transaction.atomic
def cancel_reply(*, telegram_user_id: int) -> str:
    operator = authorized_operator(telegram_user_id, lock=True)
    if operator is None:
        raise TelegramAccessDenied
    operator.reply_conversation = None
    operator.reply_started_at = None
    operator.save(update_fields=["reply_conversation", "reply_started_at", "updated_at"])
    return "Ответ отменён."


@transaction.atomic
def submit_operator_reply(*, telegram_user_id: int, update_id: int, text: str) -> str:
    """Store the reply as a pending customer message; the worker delivers it."""
    if TelegramMessage.objects.filter(telegram_update_id=update_id).exists():
        return ""
    operator = authorized_operator(telegram_user_id, lock=True)
    if operator is None:
        raise TelegramAccessDenied
    conversation = operator.reply_conversation
    started = operator.reply_started_at
    if conversation is None or started is None:
        return "Чтобы ответить клиенту, откройте заявку и нажмите «Ответить». " + OPERATOR_HELP_TEXT
    operator.reply_conversation = None
    operator.reply_started_at = None
    operator.save(update_fields=["reply_conversation", "reply_started_at", "updated_at"])
    if timezone.now() - started > REPLY_WINDOW:
        return "Режим ответа истёк. Нажмите «Ответить» ещё раз."
    conversation = TelegramConversation.objects.select_for_update().select_related(
        "request"
    ).get(pk=conversation.pk)
    reference = conversation.request.reference
    if not customer_contact_allowed(conversation.request):
        return f"По заявке {reference} клиент отозвал согласие на связь. Сообщение не отправлено."
    if not conversation.is_linked:
        return f"Клиент ещё не подключил Telegram к заявке {reference}. Сообщение не отправлено."
    text = (text or "").strip()
    if not text:
        return "Пустое сообщение не отправлено."
    if len(text) > MAX_MESSAGE_CHARS:
        return f"Сообщение длиннее {MAX_MESSAGE_CHARS} символов. Сократите его."
    now = timezone.now()
    message = TelegramMessage.objects.create(
        conversation=conversation,
        direction=TelegramMessage.Direction.OPERATOR,
        text=text,
        delivery_status=TelegramDeliveryStatus.PENDING,
        next_attempt_at=now,
        telegram_update_id=update_id,
        operator=operator,
        operator_user=operator.user,
    )
    conversation.last_message_at = now
    conversation.save(update_fields=["last_message_at", "updated_at"])
    TelegramOutboxEvent.objects.create(
        kind=TelegramOutboxEvent.Kind.OPERATOR_REPLY,
        request=conversation.request,
        message=message,
        exclude_operator=operator,
        dedupe_key=f"operator_reply:{message.pk}",
        next_attempt_at=now,
    )
    return f"Ответ по заявке {reference} поставлен в отправку клиенту."


# --- Customer actions --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CustomerResult:
    reply: str = ""
    keyboard: dict | None = None


def _linked_conversations(chat_id: int) -> list[TelegramConversation]:
    return list(
        TelegramConversation.objects.select_related("request")
        .filter(customer_chat_id=chat_id, status=TelegramConversation.Status.LINKED)
        .order_by("-linked_at", "-pk")[:20]
    )


def _selector(conversations) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"Заявка {conversation.request.reference}",
                    "callback_data": f"s:{conversation.public_id.hex}",
                }
            ]
            for conversation in conversations
        ]
    }


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
    return CustomerResult(text, _selector(still_open) if still_open else None)


def customer_greeting(chat_id: int) -> str:
    linked = _linked_conversations(chat_id)
    if messaging.open_conversations(linked):
        return LINKED_GREETING
    return messaging.NO_OPEN_REQUESTS_TEXT if linked else UNLINKED_GREETING


def customer_conversations_prompt(chat_id: int) -> CustomerResult:
    linked = _linked_conversations(chat_id)
    # A closed request is never offered, whatever an older keyboard still shows.
    conversations = messaging.open_conversations(linked)
    if not conversations:
        return CustomerResult(messaging.NO_OPEN_REQUESTS_TEXT if linked else UNLINKED_GREETING)
    state = TelegramCustomerChat.objects.filter(chat_id=chat_id).first()
    current = next(
        (c for c in conversations if state and c.pk == state.active_conversation_id), None
    )
    text = "Выберите заявку, по которой хотите написать."
    if current:
        text += f" Сейчас выбрана заявка {current.request.reference}."
    return CustomerResult(text, _selector(conversations))


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
