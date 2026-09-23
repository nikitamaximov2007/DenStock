"""What the operators' Telegram bot shows employees, for requests of both messengers.

Employees reach every customer request through one bot, whether the customer
writes in Telegram or in MAX. This module renders the texts and buttons; it
changes nothing. Reply mode itself (which request an employee answers) lives
in ``telegram_service``, next to the operator identity it belongs to.

Button data is ``kind:value`` within Telegram's 64-byte limit. A request is
named by the hex of its ``public_id`` (never the primary key). Buttons sent
before this release name a Telegram conversation instead; those still open the
same request.
"""
from __future__ import annotations

import re
import uuid
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Q
from django.urls import reverse

from apps.core.templatetags.number_format import money_int, quantity_int

from . import workspace
from .models import CustomerRequest, TelegramConversation
from .operator_replies import CHANNEL_LABELS, reply_target

HEX_RE = re.compile(r"^[0-9a-f]{32}$")
CARD_TEXT_LIMIT = 3600
LIST_PAGE_SIZE = 8
PREVIEW_CHARS = 700
NAME_CHARS = 20

ACTIVE_LIST_BUTTON = {"text": "Активные заявки", "callback_data": "l:1"}
KIND_NEW_REQUEST = "new_request"
KIND_CUSTOMER_LINKED = "customer_linked"
KIND_CUSTOMER_MESSAGE = "customer_message"
KIND_OPERATOR_REPLY = "operator_reply"


def request_by_hex(value) -> CustomerRequest | None:
    """The request a button names: its own id, or a Telegram conversation's from before."""
    if not isinstance(value, str) or not HEX_RE.fullmatch(value):
        return None
    identifier = uuid.UUID(hex=value)
    queryset = CustomerRequest.objects.prefetch_related("lines")
    request = queryset.filter(public_id=identifier).first()
    if request is not None:
        return request
    conversation = TelegramConversation.objects.filter(public_id=identifier).first()
    return queryset.filter(pk=conversation.request_id).first() if conversation else None


def channel_label(request: CustomerRequest) -> str:
    return CHANNEL_LABELS.get(request.preferred_messenger, request.preferred_messenger)


def customer_name(request: CustomerRequest) -> str:
    return "данные обезличены" if request.data_anonymized_at else request.customer_name


def display_name(user) -> str:
    if user is None:
        return "сотрудник"
    full_name = getattr(user, "full_name", "") or user.get_full_name()
    return full_name or user.get_username()


def internal_request_url(request: CustomerRequest) -> str | None:
    """The request in DenisStock. The page itself requires a signed-in employee."""
    base = settings.TELEGRAM_INTERNAL_BASE_URL
    if not base.startswith(("https://", "http://")):
        return None
    return f"{base}{reverse('customer_request_detail', args=[request.pk])}"


def quote(text: str, limit: int = PREVIEW_CHARS) -> str:
    text = (text or "").strip()
    if not text:
        return "«…»"
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return f"«{text}»"


def _link_state(target) -> str:
    return "подключён" if target.linked else "ожидает подключения"


def request_buttons(request: CustomerRequest, *, target=None) -> dict:
    """[Ответить] only when the customer can be answered now; always the way out."""
    target = target or reply_target(request)
    first_row = []
    if not target.blocked_reason():
        first_row.append({"text": "Ответить", "callback_data": f"r:{request.public_id.hex}"})
    url = internal_request_url(request)
    if url:
        first_row.append({"text": "Открыть заявку", "url": url})
    rows = [first_row] if first_row else []
    rows.append([ACTIVE_LIST_BUTTON])
    return {"inline_keyboard": rows}


def card_text(request: CustomerRequest, *, heading: str = "ЗАЯВКА", target=None) -> str:
    """The request as stored: its line snapshots, never today's catalog."""
    target = target or reply_target(request)
    lines = list(request.lines.all())
    out = [
        f"{heading} №{request.reference} · {target.label}",
        f"Статус: {request.get_status_display()}",
    ]
    if request.data_anonymized_at:
        out.append("Клиент: данные обезличены")
    else:
        out.append(f"Клиент: {request.customer_name}")
        out.append(f"Телефон: {request.customer_phone}")
    out.append(f"Связь: {target.label}, {_link_state(target)}")
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


def notification(kind: str, request: CustomerRequest, message=None) -> tuple[str, dict]:
    """One operator notification, the same for a Telegram and a MAX customer.

    The request number, the messenger and the customer come first, so an
    employee sees at a glance whom it is about. Rendered at send time: a
    request closed since the event shows no [Ответить].
    """
    target = reply_target(request)
    label = target.label
    reference = request.reference
    buttons = request_buttons(request, target=target)
    if kind == KIND_NEW_REQUEST:
        return card_text(request, heading="НОВАЯ ЗАЯВКА", target=target), buttons
    header = f"Заявка №{reference} · {label}\nКлиент: {customer_name(request)}"
    if kind == KIND_CUSTOMER_LINKED:
        return f"{header}\n\nКлиент подключил {label} к заявке №{reference}.", buttons
    text = message.text if message is not None else ""
    if kind == KIND_CUSTOMER_MESSAGE:
        return f"{header}\n\nНовое сообщение клиента:\n{quote(text)}", buttons
    if kind == KIND_OPERATOR_REPLY:
        author = display_name(message.operator_user if message is not None else None)
        return (
            f"{header}\n\nОтвет клиенту отправлен.\nСотрудник: {author}\n{quote(text)}",
            buttons,
        )
    return f"Заявка №{reference}: переписка недоступна.", buttons


def menu() -> tuple[str, dict]:
    return (
        "Бот заявок PRO-STOR. Сюда приходят новые заявки и сообщения клиентов из Telegram и MAX.",
        {"inline_keyboard": [[ACTIVE_LIST_BUTTON]]},
    )


def _row_state(request) -> str:
    if request.attention == workspace.ATTENTION_FAILED:
        return "ответ не доставлен"
    if request.attention == workspace.ATTENTION_SENDING:
        return "ответ отправляется"
    if request.needs_reply:
        return "ждёт ответа"
    if request.status == CustomerRequest.Status.NEW:
        return "новая"
    return "в работе"


def request_page(page: int, *, new_only: bool = False) -> tuple[str, dict | None]:
    """Open requests of both messengers, optionally limited to new requests."""
    queryset = workspace.annotate_workspace().filter(status__in=workspace.OPEN_STATUSES)
    if new_only:
        queryset = queryset.filter(status=CustomerRequest.Status.NEW)
    queryset = workspace.order_by_priority(queryset)
    counts = queryset.order_by().aggregate(
        total=Count("pk"),
        waiting=Count("pk", filter=Q(needs_reply=True)),
    )
    total = counts["total"]
    if not total:
        return "Активных заявок нет.", None
    pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
    page = min(max(1, page), pages)
    start = (page - 1) * LIST_PAGE_SIZE
    rows = []
    for request in queryset[start : start + LIST_PAGE_SIZE]:
        marker = "● " if request.needs_reply else ""
        name = customer_name(request)[:NAME_CHARS]
        rows.append(
            [
                {
                    "text": (
                        f"{marker}№{request.reference} · {channel_label(request)} · "
                        f"{name} · {_row_state(request)}"
                    ),
                    "callback_data": f"c:{request.public_id.hex}",
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
    heading = (
        f"Новые заявки: {total}"
        if new_only
        else f"Активные заявки: {total} · ждут ответа: {counts['waiting']}"
    )
    if pages > 1:
        heading += f"\nСтраница {page} из {pages}"
    return heading, {"inline_keyboard": rows}


def reply_prompt(request: CustomerRequest, *, target, last_message) -> tuple[str, dict]:
    """Reply mode, stated so plainly the employee cannot mistake the recipient."""
    if last_message is not None and last_message.text:
        last = f"Последнее сообщение клиента:\n{quote(last_message.text)}"
    else:
        last = "Клиент пока ничего не написал."
    text = (
        f"Ответ на заявку №{request.reference} · {target.label}\n"
        f"Клиент: {customer_name(request)}\n\n"
        f"{last}\n\n"
        "Напишите ответ одним сообщением. Клиент увидит его от имени бота PRO-STOR."
    )
    cancel = {"text": "Отмена", "callback_data": "x"}
    return text, {"inline_keyboard": [[cancel, {"text": "К заявкам", "callback_data": "l:1"}]]}


def after_reply_buttons(request: CustomerRequest) -> dict:
    row = []
    url = internal_request_url(request)
    if url:
        row.append({"text": "Открыть заявку", "url": url})
    row.append({"text": "К заявкам", "callback_data": "l:1"})
    return {"inline_keyboard": [row]}


def back_to_list() -> dict:
    return {"inline_keyboard": [[ACTIVE_LIST_BUTTON]]}
