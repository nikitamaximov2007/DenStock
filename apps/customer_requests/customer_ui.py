"""What a customer sees and chooses, written once for both messengers.

A customer knows their own orders, not our commands. Everything they need is a
button: «Мои заявки» opens their active requests, one press chooses the one they
are writing about, and the rest is plain text. Commands still work, but nobody
has to learn them.

This module owns the rules and the words:

* which of a customer's requests are theirs to write about now (Stage 0's
  messageability, unchanged);
* which one is currently selected, and how that is shown;
* what the selector says when there are none, one, or several;
* the greeting a newly linked request gets, once.

It never touches a transport: a caller passes the conversations it already
loaded and stores the result in its own rows. What differs between MAX and
Telegram is how a button is drawn, not what it means.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apps.core.templatetags.number_format import money_int

from . import messaging
from .models import CustomerRequest

# The one entry point a customer needs. Telegram shows it on a persistent
# keyboard, MAX as an inline button under the bot's own messages.
MY_REQUESTS_BUTTON = "Мои заявки"
MY_REQUESTS_PAYLOAD = "menu"
MY_PURCHASES_BUTTON = "Мои покупки"
MY_PURCHASES_PAYLOAD = "purchases"
SELECT_PAYLOAD_PREFIX = "s:"
PURCHASE_PAYLOAD_PREFIX = "p:"
REORDER_PAYLOAD_PREFIX = "r:"
REORDER_CONFIRM_PAYLOAD_PREFIX = "rc:"
CURRENT_MARK = "✓"

GREETING_TEXT = "Добрый день! Ваша заявка №{reference} получена."
GREETING_CLOSING = (
    "Если у вас есть вопросы по заявке, напишите нам здесь — менеджер ответит вам."
)
SELECTED_TEXT = "Выбрана заявка №{reference}."
SELECTOR_HEADING = "Мои активные заявки:"
SELECTOR_ONE_HEADING = "Ваша активная заявка:"
SELECTOR_HINT = "Выберите заявку, по которой хотите написать."
NO_ACTIVE_REQUESTS_TEXT = messaging.NO_OPEN_REQUESTS_TEXT
UNKNOWN_PRICE_TEXT = "Цена уточняется"
PARTIAL_PRICE_SUFFIX = "цена части позиций уточняется"
LINKED_HINT = "Напишите сообщение — менеджер PRO-STOR ответит вам здесь."


def plural(number: int, one: str, few: str, many: str) -> str:
    """Russian plural of a count: 1 позиция, 2 позиции, 5 позиций."""
    tail, hundred = number % 10, number % 100
    if tail == 1 and hundred != 11:
        return one
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return few
    return many


def positions_text(count: int) -> str:
    return f"{count} {plural(count, 'позиция', 'позиции', 'позиций')}"


def purchase_summary_text(purchase) -> str:
    """Compact safe purchase row; ``purchase`` is a cabinet DTO."""
    date = purchase.sold_at.strftime("%d.%m.%Y") if purchase.sold_at else "дата не указана"
    return (
        f"Покупка №{purchase.number}\n{date}\n"
        f"{positions_text(len(purchase.lines))} · {money_int(purchase.total)} ₽"
    )


def purchase_detail_text(purchase) -> str:
    rows = [f"Покупка №{purchase.number}"]
    for line in purchase.lines:
        rows.append(f"\n{line.name}")
        rows.append(f"{_quantity_text(line.quantity)} × {money_int(line.unit_price)} ₽")
    rows.append(f"\nИтого: {money_int(purchase.total)} ₽")
    return "\n".join(rows)


def _quantity_text(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def reorder_preview_text(preview) -> str:
    rows = [f"Повтор покупки №{preview.purchase.number}"]
    for line in preview.lines:
        if not line.available:
            rows.append(f"\n{line.name}\n{line.reason}")
            continue
        quantity = _quantity_text(line.requested_quantity)
        if line.current_unit_price is None:
            current = UNKNOWN_PRICE_TEXT
        else:
            current = f"{quantity} × {money_int(line.current_unit_price)} ₽"
        stock = (
            "Запрос о поставке"
            if line.supply_inquiry
            else f"В наличии: {_quantity_text(line.available_quantity)}"
        )
        rows.append(f"\n{line.name}\n{current}\n{stock}")
    total = preview.total
    rows.append(
        f"\nИтого доступных позиций: {money_int(total)} ₽"
        if total is not None
        else "\nИтого: цена уточняется"
    )
    return "\n".join(rows)


@dataclass(frozen=True, slots=True)
class RequestMoney:
    """A request's money as the customer was shown it, never an unknown as zero."""

    known: Decimal | None
    unknown_count: int

    def text(self) -> str:
        if self.known is None:
            return UNKNOWN_PRICE_TEXT
        if self.unknown_count:
            return f"{money_int(self.known)} ₽ · {PARTIAL_PRICE_SUFFIX}"
        return f"{money_int(self.known)} ₽"


def request_money(request: CustomerRequest) -> RequestMoney:
    known: Decimal | None = None
    unknown = 0
    for line in request.lines.all():
        if line.price_seen is None:
            unknown += 1
            continue
        known = (known or Decimal("0")) + line.price_seen * line.quantity_requested
    return RequestMoney(known=known, unknown_count=unknown)


# --- Which requests a customer may write about -------------------------------------------


def active_conversations(conversations) -> list:
    """Only the conversations whose request is still open to the customer."""
    return messaging.open_conversations(conversations)


def current_conversation(conversations, *, active_id):
    """The selected one, if that selection is still a request they may write about."""
    return next((c for c in conversations if active_id and c.pk == active_id), None)


# --- The greeting of a newly linked request ------------------------------------------------


def greeting_policy(message_limit: int) -> messaging.SummaryPolicy:
    """The first message of a request: a greeting, then its immutable summary."""
    return messaging.SummaryPolicy(linked_text=GREETING_TEXT, message_limit=message_limit)


def greeting_messages(request: CustomerRequest, message_limit: int) -> list[str]:
    return messaging.request_summary_messages(request, greeting_policy(message_limit))


# --- The selector -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectorView:
    """One rendering of «Мои заявки»: what to say and what to offer."""

    text: str
    conversations: list
    current_id: int | None

    @property
    def has_choices(self) -> bool:
        return bool(self.conversations)

    def button_labels(self) -> list[str]:
        """One label per request, the current one marked."""
        return [
            f"{CURRENT_MARK} №{c.request.reference}"
            if c.pk == self.current_id
            else f"№{c.request.reference}"
            for c in self.conversations
        ]

    def payloads(self) -> list[str]:
        return [f"{SELECT_PAYLOAD_PREFIX}{c.public_id.hex}" for c in self.conversations]

    def buttons(self) -> list[tuple[str, str]]:
        return list(zip(self.button_labels(), self.payloads(), strict=True))


def _request_line(conversation, *, current: bool) -> str:
    request = conversation.request
    mark = f"{CURRENT_MARK} " if current else "   "
    money = request_money(request).text()
    # ``lines`` is prefetched by the transports: no query per row here.
    positions = positions_text(len(request.lines.all()))
    return f"{mark}№{request.reference} · {positions}\n   {money}"


def selector_view(conversations, *, active_id, linked_any: bool = True) -> SelectorView:
    """«Мои заявки» for zero, one or several active requests.

    Closed requests are never offered, whatever an older keyboard still shows,
    and the request the customer is writing about now is marked.
    """
    active = active_conversations(conversations)
    if not active:
        text = NO_ACTIVE_REQUESTS_TEXT if linked_any else UNLINKED_TEXT
        return SelectorView(text=text, conversations=[], current_id=None)
    current = current_conversation(active, active_id=active_id)
    current_id = current.pk if current is not None else None
    heading = SELECTOR_ONE_HEADING if len(active) == 1 else SELECTOR_HEADING
    rows = [_request_line(c, current=c.pk == current_id) for c in active]
    text = "\n\n".join([heading, *rows])
    if len(active) > 1:
        text += f"\n\n{SELECTOR_HINT}"
    elif current_id is None:
        text += f"\n\n{SELECTOR_HINT}"
    else:
        text += f"\n\n{LINKED_HINT}"
    return SelectorView(text=text, conversations=active, current_id=current_id)


UNLINKED_TEXT = (
    "Это бот PRO-STOR для связи по заявкам. Чтобы написать менеджеру, оформите заявку "
    "на сайте PRO-STOR и откройте ссылку из неё."
)


def selected_text(reference: str) -> str:
    """Switching requests is a one-line confirmation, never the whole greeting."""
    return SELECTED_TEXT.format(reference=reference)
