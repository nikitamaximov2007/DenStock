"""What an employee needs to know about customer requests, derived from history.

Nothing here is stored. «Ждёт ответа» is read from the messages themselves,
so it cannot drift from what the customer and the employees actually wrote.

The rule (``needs_reply``), for one request:

* the request is open (``new`` or ``in_progress``). A closed request waits for
  nobody: its customer can no longer write, and it is history;
* the customer wrote at least one message (``customer_to_operator``) in either
  messenger;
* and no employee reply was *delivered* after the customer's latest message:
  there is no ``operator_to_customer`` message with status ``sent`` created
  later than that customer message.

Only a delivered employee reply answers. A reply still queued or being sent
(``pending``/``sending``) does not clear the state yet: it is shown as «Ответ
отправляется» until the worker delivers it. The message-level status keeps the
distinction: ``pending`` means queued for a retry and ``sending`` means the
worker is currently attempting delivery. One the messenger refused
(``failed``) or one whose fate is unknown (``uncertain``) never clears it: it is
shown as «Ответ не доставлен», because the customer may never have seen it.
Bot messages (``system``: summaries, acknowledgements, selectors) are not
replies and are ignored, as are the customer's questions the bot declined to
store (a closed request, an ambiguous choice): those never became messages.
Times compare creation of the messages, so a customer who writes again while an
answer is still on its way is waiting again once that answer lands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    DateTimeField,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.phones import normalize_phone

from .models import (
    CustomerRequest,
    CustomerRequestLine,
    MaxConversation,
    MaxMessage,
    TelegramConversation,
    TelegramMessage,
)

OPEN_STATUSES = (CustomerRequest.Status.NEW, CustomerRequest.Status.IN_PROGRESS)
CUSTOMER = "customer_to_operator"
OPERATOR = "operator_to_customer"
IN_FLIGHT = ("pending", "sending")
NOT_DELIVERED = ("failed", "uncertain")

# Attention, strongest first. Stored nowhere; see the module docstring.
ATTENTION_FAILED = "reply_failed"
ATTENTION_SENDING = "reply_sending"
ATTENTION_WAITING = "waiting"
ATTENTION_NONE = ""

PRIORITY_WAITING = 0
PRIORITY_NEW = 1
PRIORITY_IN_PROGRESS = 2
PRIORITY_CLOSED = 3

TAB_ACTIVE = "active"
TAB_WAITING = "waiting"
TAB_NEW = "new"
TAB_COMPLETED = "completed"
TAB_CANCELED = "canceled"
TAB_ALL = "all"
TABS = (
    (TAB_ACTIVE, "Активные"),
    (TAB_WAITING, "Ждут ответа"),
    (TAB_NEW, "Новые"),
    (TAB_COMPLETED, "Завершённые"),
    (TAB_CANCELED, "Отменённые"),
    (TAB_ALL, "Все"),
)
MESSENGER_ALL = ""
MESSENGERS = (
    (MESSENGER_ALL, "Все мессенджеры"),
    (CustomerRequest.Messenger.MAX, "MAX"),
    (CustomerRequest.Messenger.TELEGRAM, "Telegram"),
)
REFERENCE_RE = re.compile(r"^№?\s*(\d{1,12})$")
SEARCH_MAX_CHARS = 100
PREVIEW_CHARS = 140


def _latest(model, *, direction: str, statuses=None, field: str = "created_at"):
    """The newest value of ``field`` among one request's messages in one transport."""
    messages = model.objects.filter(conversation__request=OuterRef("pk"), direction=direction)
    if statuses:
        messages = messages.filter(delivery_status__in=statuses)
    return Subquery(messages.order_by("-created_at", "-pk").values(field)[:1])


def _later(first: str, second: str):
    """The later of two nullable timestamps, the same on PostgreSQL and SQLite."""
    return Case(
        When(**{f"{first}__isnull": True}, then=F(second)),
        When(**{f"{second}__isnull": True}, then=F(first)),
        When(**{f"{first}__gte": F(second)}, then=F(first)),
        default=F(second),
        output_field=DateTimeField(),
    )


def _after(later: str, earlier: str) -> Q:
    """``later`` exists and is strictly after ``earlier`` (or ``earlier`` is empty)."""
    return Q(**{f"{later}__isnull": False}) & (
        Q(**{f"{earlier}__isnull": True}) | Q(**{f"{later}__gt": F(earlier)})
    )


def annotate_workspace(queryset=None):
    """Every column the list, the bot and the ordering need, in one query."""
    queryset = CustomerRequest.objects.all() if queryset is None else queryset
    lines = CustomerRequestLine.objects.filter(request=OuterRef("pk")).order_by().values("request")
    money = DecimalField(max_digits=16, decimal_places=2)
    queryset = queryset.annotate(
        tg_customer_at=_latest(TelegramMessage, direction=CUSTOMER),
        max_customer_at=_latest(MaxMessage, direction=CUSTOMER),
        tg_customer_text=_latest(TelegramMessage, direction=CUSTOMER, field="text"),
        max_customer_text=_latest(MaxMessage, direction=CUSTOMER, field="text"),
        tg_sent_at=_latest(TelegramMessage, direction=OPERATOR, statuses=["sent"]),
        max_sent_at=_latest(MaxMessage, direction=OPERATOR, statuses=["sent"]),
        tg_flight_at=_latest(TelegramMessage, direction=OPERATOR, statuses=IN_FLIGHT),
        max_flight_at=_latest(MaxMessage, direction=OPERATOR, statuses=IN_FLIGHT),
        tg_failed_at=_latest(TelegramMessage, direction=OPERATOR, statuses=NOT_DELIVERED),
        max_failed_at=_latest(MaxMessage, direction=OPERATOR, statuses=NOT_DELIVERED),
        tg_last_message_at=Subquery(
            TelegramConversation.objects.filter(request=OuterRef("pk")).values("last_message_at")[:1]
        ),
        max_last_message_at=Subquery(
            MaxConversation.objects.filter(request=OuterRef("pk")).values("last_message_at")[:1]
        ),
        tg_linked=Exists(
            TelegramConversation.objects.filter(
                request=OuterRef("pk"),
                status=TelegramConversation.Status.LINKED,
                customer_chat_id__isnull=False,
            )
        ),
        max_linked=Exists(
            MaxConversation.objects.filter(
                request=OuterRef("pk"),
                status=MaxConversation.Status.LINKED,
                customer_user_id__isnull=False,
                customer_chat_id__isnull=False,
            )
        ),
        line_count=Coalesce(
            Subquery(lines.annotate(n=Count("pk")).values("n")[:1]), Value(0)
        ),
        unknown_price_count=Coalesce(
            Subquery(
                lines.filter(price_seen__isnull=True).annotate(n=Count("pk")).values("n")[:1]
            ),
            Value(0),
        ),
        known_total=Subquery(
            lines.filter(price_seen__isnull=False)
            .annotate(
                total=Sum(
                    ExpressionWrapper(F("price_seen") * F("quantity_requested"), output_field=money)
                )
            )
            .values("total")[:1],
            output_field=money,
        ),
    ).annotate(
        last_customer_at=_later("tg_customer_at", "max_customer_at"),
        last_sent_reply_at=_later("tg_sent_at", "max_sent_at"),
        last_flight_reply_at=_later("tg_flight_at", "max_flight_at"),
        last_failed_reply_at=_later("tg_failed_at", "max_failed_at"),
        conversation_activity_at=_later("tg_last_message_at", "max_last_message_at"),
        customer_preview=Case(
            When(max_customer_at__isnull=True, then=F("tg_customer_text")),
            When(tg_customer_at__isnull=True, then=F("max_customer_text")),
            When(tg_customer_at__gte=F("max_customer_at"), then=F("tg_customer_text")),
            default=F("max_customer_text"),
            output_field=CharField(),
        ),
        is_linked=Case(
            When(Q(tg_linked=True) | Q(max_linked=True), then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
    ).annotate(
        activity_at=_later("updated_at", "conversation_activity_at"),
        needs_reply=Case(
            When(
                Q(status__in=OPEN_STATUSES) & _after("last_customer_at", "last_sent_reply_at"),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        ),
    ).annotate(
        attention=Case(
            When(
                Q(needs_reply=True) & _after("last_failed_reply_at", "last_customer_at")
                & ~_after("last_flight_reply_at", "last_failed_reply_at"),
                then=Value(ATTENTION_FAILED),
            ),
            When(
                Q(needs_reply=True) & _after("last_flight_reply_at", "last_customer_at"),
                then=Value(ATTENTION_SENDING),
            ),
            When(needs_reply=True, then=Value(ATTENTION_WAITING)),
            default=Value(ATTENTION_NONE),
            output_field=CharField(),
        ),
        priority=Case(
            When(needs_reply=True, then=Value(PRIORITY_WAITING)),
            When(status=CustomerRequest.Status.NEW, then=Value(PRIORITY_NEW)),
            When(status=CustomerRequest.Status.IN_PROGRESS, then=Value(PRIORITY_IN_PROGRESS)),
            default=Value(PRIORITY_CLOSED),
            output_field=IntegerField(),
        ),
    )
    return queryset


def order_by_priority(queryset):
    """Most actionable first.

    Waiting for a reply, longest wait first (the customer who has waited the
    most is answered next); then new requests; then those in work; then closed
    history. Within each of the last three, the most recent activity first.
    """
    return queryset.order_by(
        "priority",
        Case(
            When(priority=PRIORITY_WAITING, then=F("last_customer_at")),
            default=None,
            output_field=DateTimeField(),
        ).asc(nulls_last=True),
        F("activity_at").desc(nulls_last=True),
        "-pk",
    )


def tab_filter(tab: str) -> Q:
    if tab == TAB_WAITING:
        return Q(needs_reply=True)
    if tab == TAB_NEW:
        return Q(status=CustomerRequest.Status.NEW)
    if tab == TAB_COMPLETED:
        return Q(status=CustomerRequest.Status.COMPLETED)
    if tab == TAB_CANCELED:
        return Q(status=CustomerRequest.Status.CANCELED)
    if tab == TAB_ALL:
        return Q()
    return Q(status__in=OPEN_STATUSES)


def clean_tab(value) -> str:
    return value if value in dict(TABS) else TAB_ACTIVE


def clean_messenger(value) -> str:
    return value if value in dict(MESSENGERS) else MESSENGER_ALL


def clean_query(value) -> str:
    return str(value or "").strip()[:SEARCH_MAX_CHARS]


def _text_variants(value: str) -> list[str]:
    """The spellings to look for, because no database folds Cyrillic case for us.

    SQLite's LIKE folds ASCII only, and PostgreSQL's UPPER leaves Cyrillic
    untouched in the C locale this cluster runs (see
    ``apps.core.part_lookup``), so ``icontains`` is effectively case-sensitive
    for Russian on both. Matching the few spellings a person actually types
    keeps the search honest without a new stored column.
    """
    spellings = [value, value.lower(), value.upper(), value.capitalize(), value.title()]
    return list(dict.fromkeys(spellings))


def _contains_any(field: str, value: str) -> Q:
    condition = Q()
    for variant in _text_variants(value):
        condition |= Q(**{f"{field}__contains": variant})
    return condition


def search_filter(query: str) -> Q:
    """Request number, customer name, phone, article or part name.

    Everything a customer reads out on the phone. The request table is small
    (requests from the public catalog, not stock), and its phone is indexed in
    a normalized form; line search only runs for three characters or more.
    """
    query = clean_query(query)
    if not query:
        return Q()
    condition = _contains_any("customer_name", query)
    reference = REFERENCE_RE.fullmatch(query)
    if reference:
        condition |= Q(human_number=int(reference.group(1)))
    legacy_reference = re.fullmatch(r"[0-9a-fA-F]{4,8}", query)
    if legacy_reference:
        condition |= Q(public_id__istartswith=query.lower())
    digits = normalize_phone(query)
    if len(re.sub(r"\D", "", query)) >= 4 and digits:
        condition |= Q(customer_phone_normalized__contains=digits)
    if len(query) >= 3:
        lines = CustomerRequestLine.objects.filter(request=OuterRef("pk"))
        condition |= Exists(
            lines.filter(_contains_any("article", query) | _contains_any("part_name", query))
        )
    return condition


def filtered_requests(*, tab: str, messenger: str, query: str):
    """The annotated, filtered and ordered list the employee asked for."""
    queryset = annotate_workspace()
    if messenger:
        queryset = queryset.filter(preferred_messenger=messenger)
    queryset = queryset.filter(search_filter(query))
    return order_by_priority(queryset.filter(tab_filter(tab)))


def tab_counts(*, messenger: str, query: str) -> dict[str, int]:
    queryset = annotate_workspace()
    if messenger:
        queryset = queryset.filter(preferred_messenger=messenger)
    queryset = queryset.filter(search_filter(query))
    return queryset.aggregate(
        **{
            TAB_ACTIVE: Count("pk", filter=Q(status__in=OPEN_STATUSES)),
            TAB_WAITING: Count("pk", filter=Q(needs_reply=True)),
            TAB_NEW: Count("pk", filter=Q(status=CustomerRequest.Status.NEW)),
            TAB_COMPLETED: Count("pk", filter=Q(status=CustomerRequest.Status.COMPLETED)),
            TAB_CANCELED: Count("pk", filter=Q(status=CustomerRequest.Status.CANCELED)),
            TAB_ALL: Count("pk"),
        }
    )


# --- Money ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestTotal:
    """A request's total as the customer was shown it: never an unknown as zero."""

    known: Decimal | None
    unknown_count: int

    @property
    def complete(self) -> bool:
        return self.known is not None and self.unknown_count == 0

    @property
    def partial(self) -> bool:
        return self.known is not None and self.unknown_count > 0


def request_total(*, known_total, unknown_price_count) -> RequestTotal:
    return RequestTotal(known=known_total, unknown_count=int(unknown_price_count or 0))


def lines_total(lines) -> RequestTotal:
    known = None
    unknown = 0
    for line in lines:
        if line.price_seen is None:
            unknown += 1
            continue
        known = (known or Decimal("0")) + line.price_seen * line.quantity_requested
    return RequestTotal(known=known, unknown_count=unknown)


# --- One request ----------------------------------------------------------------------


def annotated_request(pk: int) -> CustomerRequest | None:
    return annotate_workspace(CustomerRequest.objects.filter(pk=pk)).first()


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    role: str  # "customer", "operator" or "bot"
    author: str
    text: str
    created_at: object
    delivery_status: str
    delivery_label: str
    channel: str
    date_separator: object = None

    @property
    def delivered(self) -> bool:
        return self.delivery_status in {"sent", "received"}

    @property
    def not_delivered(self) -> bool:
        return self.delivery_status in NOT_DELIVERED

    @property
    def in_flight(self) -> bool:
        # ``pending`` is a bounded retry wait, not an active network attempt.
        # Keeping it separate prevents the UI from looking stuck on
        # «Отправляется…» while MAX is waiting for attachment processing.
        return self.delivery_status == "sending"


TIMELINE_LIMIT = 300
ROLE_BY_DIRECTION = {CUSTOMER: "customer", OPERATOR: "operator", "system": "bot"}


def _author(message) -> str:
    if message.direction == CUSTOMER:
        request = getattr(getattr(message, "conversation", None), "request", None)
        return (getattr(request, "customer_name", "") or "Клиент").strip()
    if message.direction == OPERATOR:
        user = message.operator_user
        if user is None:
            return "Сотрудник"
        return getattr(user, "full_name", "") or user.get_full_name() or user.get_username()
    return "Бот PRO-STOR"


def timeline(request: CustomerRequest) -> list[TimelineEntry]:
    """The request's conversation in both messengers, oldest first.

    Only what belongs to the request: bot answers sent to a chat before it
    chose a request (a selector, a closed-request notice) are not part of any
    request's conversation and are not shown here.
    """
    entries = []
    sources = (
        ("Telegram", TelegramMessage.objects.filter(conversation__request=request)),
        ("MAX", MaxMessage.objects.filter(conversation__request=request)),
    )
    for channel, messages in sources:
        newest = messages.select_related("operator_user", "conversation__request").order_by(
            "-created_at", "-pk"
        )
        for message in newest[:TIMELINE_LIMIT]:
            entries.append(
                (
                    message.created_at,
                    message.pk,
                    TimelineEntry(
                        role=ROLE_BY_DIRECTION.get(message.direction, "bot"),
                        author=_author(message),
                        text=message.text,
                        created_at=message.created_at,
                        delivery_status=message.delivery_status,
                        delivery_label=message.get_delivery_status_display(),
                        channel=channel,
                    ),
                )
            )
    entries.sort(key=lambda item: (item[0], item[1]))
    result = []
    previous_day = None
    for _created, _pk, entry in entries[-TIMELINE_LIMIT:]:
        day = timezone.localtime(entry.created_at).date()
        separator = day if day != previous_day else None
        result.append(
            TimelineEntry(
                role=entry.role,
                author=entry.author,
                text=entry.text,
                created_at=entry.created_at,
                delivery_status=entry.delivery_status,
                delivery_label=entry.delivery_label,
                channel=entry.channel,
                date_separator=separator,
            )
        )
        previous_day = day
    return result


def latest_customer_message(request: CustomerRequest):
    """The customer's newest message in either messenger, or None."""
    candidates = [
        TelegramMessage.objects.filter(conversation__request=request, direction=CUSTOMER)
        .order_by("-created_at", "-pk")
        .first(),
        MaxMessage.objects.filter(conversation__request=request, direction=CUSTOMER)
        .order_by("-created_at", "-pk")
        .first(),
    ]
    candidates = [message for message in candidates if message is not None]
    return max(candidates, key=lambda message: message.created_at, default=None)
