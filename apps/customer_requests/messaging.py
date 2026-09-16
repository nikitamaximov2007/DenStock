"""Customer messaging rules that belong to the request, not to a messenger.

Telegram proved these rules in production; MAX will need exactly the same ones.
What lives here is the part a customer would notice if it changed: what the
summary says, when the bot acknowledges a message, and which request a plain
message belongs to. What stays in a transport is how bytes reach a person.

Nothing here touches a transport model, so a caller passes in what it already
knows and stores the outcome in its own tables. That keeps Telegram's proven
schema and history untouched while a second transport reuses the behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apps.core.templatetags.number_format import money_int, quantity_int

from .models import CustomerRequest

# The bot says this once per request conversation, never on later messages.
CUSTOMER_ACK_TEXT = "Сообщение передано менеджеру PRO-STOR."

UNKNOWN_PRICE_TEXT = "цена уточняется"
CONTINUATION_HEADING = "Ваш заказ (продолжение):"
ORDER_HEADING = "Ваш заказ:"
CLOSING_TEXT = "Менеджер PRO-STOR ответит вам здесь.\nМожете написать вопрос прямо сейчас."


@dataclass(frozen=True, slots=True)
class SummaryPolicy:
    """What one transport can carry, and how it names itself to the customer.

    ``message_limit`` is the transport's own maximum, never a constant of the
    domain: Telegram and MAX happen to agree on 4000 today, and a transport
    that disagrees tomorrow must not need a change here.
    """

    linked_text: str
    message_limit: int


def summary_line(line) -> tuple[str, Decimal | None]:
    """One complete order line from the request's own immutable snapshots.

    ``price_seen`` is what the customer was shown when they submitted. The
    catalog price may have moved since; quoting it back would be a different
    promise from the one they accepted.
    """
    article = line.article or "Артикул уточняется"
    name = line.part_name or "Название уточняется"
    unit = (line.unit_short_name or "").strip()
    if unit == "шт":
        unit = "шт."
    quantity = f"{quantity_int(line.quantity_requested)} {unit}".strip()
    if line.price_seen is None:
        return f"{article} — {name}\n{quantity} — {UNKNOWN_PRICE_TEXT}", None
    total = line.quantity_requested * line.price_seen
    return (
        f"{article} — {name}\n{quantity} × {money_int(line.price_seen)} ₽ = {money_int(total)} ₽",
        total,
    )


def request_summary_messages(request: CustomerRequest, policy: SummaryPolicy) -> list[str]:
    """Render an immutable request snapshot into safely sized messages.

    A complete order line is never split or dropped, so a long request simply
    spans several messages. A total is only claimed when every line has a
    price: "Итого: 0 ₽" would read as free, not as unknown.
    """
    lines = list(request.lines.order_by("pk"))
    heading = policy.linked_text.format(reference=request.reference) + f"\n\n{ORDER_HEADING}"
    messages: list[str] = []
    current = heading
    known_total = Decimal("0")
    known_price_count = 0
    has_unknown = False

    for line in lines:
        rendered, total = summary_line(line)
        if total is None:
            has_unknown = True
        else:
            known_total += total
            known_price_count += 1
        candidate = f"{current}\n\n{rendered}"
        if len(candidate) <= policy.message_limit:
            current = candidate
            continue
        messages.append(current)
        current = f"{CONTINUATION_HEADING}\n\n{rendered}"

    if has_unknown:
        total_text = (
            f"Итого по позициям с известной ценой: {money_int(known_total)} ₽\n"
            "Есть позиции, цена которых уточняется."
            if known_price_count
            else "Есть позиции, цена которых уточняется."
        )
    else:
        total_text = f"Итого: {money_int(known_total)} ₽"
    closing = f"{total_text}\n\n{CLOSING_TEXT}"
    candidate = f"{current}\n\n{closing}"
    if len(candidate) <= policy.message_limit:
        messages.append(candidate)
    else:
        messages.extend([current, closing])
    return messages


def acknowledgement_for(*, is_first_customer_message: bool) -> str:
    """The bot confirms receipt once per request, then stays out of the way.

    The caller decides "first" from persisted messages under its own lock, so a
    replayed update, a retry or a restarted worker cannot turn a later message
    into a first one.
    """
    return CUSTOMER_ACK_TEXT if is_first_customer_message else ""


@dataclass(frozen=True, slots=True)
class Routing:
    """Which of a customer's requests a plain message belongs to."""

    conversation: object | None
    ambiguous: bool

    @property
    def resolved(self) -> bool:
        return self.conversation is not None


def route_customer_message(conversations, *, active_id) -> Routing:
    """Pick the request a message is for, or refuse to pick one.

    One request is unambiguous. Several are not, and a message put on the
    wrong request is worse than a question: the customer is asked which one
    and nothing is stored until they say.
    """
    conversations = list(conversations)
    if not conversations:
        return Routing(None, ambiguous=False)
    active = next((c for c in conversations if active_id and c.pk == active_id), None)
    if active is not None:
        return Routing(active, ambiguous=False)
    if len(conversations) == 1:
        return Routing(conversations[0], ambiguous=False)
    return Routing(None, ambiguous=True)


def customer_contact_allowed(request: CustomerRequest) -> bool:
    """A withdrawn consent or an anonymized request ends the conversation."""
    return request.consent_withdrawn_at is None and request.data_anonymized_at is None


def external_message_key(channel: str, external_id) -> str:
    """A transport-scoped identity for one inbound event.

    Telegram numbers its updates; MAX identifies a message by an opaque string
    and numbers nothing. A shared key is therefore text, and carries its
    channel so two transports can never collide on the same value.
    """
    value = str(external_id or "").strip()
    if not value:
        raise ValueError("External message id is required.")
    return f"{channel}:{value}"


# What to do with one operator notification event. Production proved the zero
# recipient case for Telegram (an operator reply seen by nobody else); MAX
# notifies the same employees and must end such an event the same way.
EVENT_DELIVER = "deliver"
EVENT_COMPLETE = "complete"
EVENT_WAIT = "wait"
EVENT_EXPIRE = "expire"


def operator_event_outcome(
    *, has_recipients: bool, excludes_author: bool, anyone_eligible: bool, expired: bool
) -> str:
    """Deliver, finish with nobody to tell, keep waiting, or give up.

    An event that excludes its own author has no audience once that author is
    the only eligible employee: nobody needs to hear about their own reply,
    and waiting cannot change that, so it completes with no delivery rows.
    Only an event nobody can receive *yet* waits, until it is too old.
    """
    if has_recipients:
        return EVENT_DELIVER
    if excludes_author and anyone_eligible:
        return EVENT_COMPLETE
    return EVENT_EXPIRE if expired else EVENT_WAIT
