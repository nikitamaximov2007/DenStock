"""What a signed-in customer may see about their own requests and purchases.

Two sources, never mixed:

* **Заявки** — ``CustomerRequest`` rows owned by the account. A request is a
  wish; it is never shown as a purchase.
* **Покупки** — completed DenisStock ``Sale`` documents of the client card an
  EMPLOYEE explicitly linked to this account. Prices are the historical
  ``SaleLine`` values; cost, profit, supplier and employee data are never read.

On PostgreSQL the public role reads both through the ``customer_account_*``
views, which filter by the session bound to the transaction
(``db_security.account_transaction``); on SQLite the same filters are applied
here. The parity test in ``tests/test_customer_account_postgresql.py`` compares
the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.db import connection

ZERO = Decimal("0")


class UnboundAccountRead(RuntimeError):
    """A PostgreSQL read was asked for an account the transaction is not bound to.

    On PostgreSQL the ``customer_account_*`` views answer for whatever session
    ``account_transaction`` bound, and for nobody at all when nothing is bound.
    Without this guard a caller that forgot to bind would silently receive an
    empty list — "you have no purchases" — instead of an error, and a caller
    that bound the WRONG session would receive someone else's rows. Both are
    refused here, so the SQLite and PostgreSQL readers cannot drift apart.
    """


def _bound_account_id() -> int | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT customer_account_current()")
        row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _require_bound(account) -> None:
    """PostgreSQL only: the bound session must be this account's own."""
    bound = _bound_account_id()
    if bound != account.pk:
        raise UnboundAccountRead(
            f"account {account.pk} read inside a transaction bound to {bound!r}"
        )

ACTIVE_STATUSES = {"new", "in_progress"}
STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "completed": "Выполнена",
    "canceled": "Отменена",
}
MESSENGER_LABELS = {"telegram": "Telegram", "max": "MAX"}


@dataclass(frozen=True)
class RequestLine:
    article: str
    name: str
    quantity: Decimal
    unit: str
    price_seen: Decimal | None
    is_supply_inquiry: bool

    @property
    def total(self) -> Decimal | None:
        return None if self.price_seen is None else self.price_seen * self.quantity


@dataclass
class RequestSummary:
    id: int
    public_id: UUID
    number: str
    status: str
    messenger: str
    created_at: datetime
    lines: list[RequestLine] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def messenger_label(self) -> str:
        return MESSENGER_LABELS.get(self.messenger, self.messenger)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def known_total(self) -> Decimal | None:
        """Sum of the known prices, or None when none is known — never a false 0."""
        totals = [line.total for line in self.lines if line.total is not None]
        return sum(totals, ZERO) if totals else None

    @property
    def has_unknown_price(self) -> bool:
        return any(line.price_seen is None for line in self.lines)


def _number(human_number, public_id) -> str:
    if human_number is not None:
        return str(human_number)
    from apps.customer_requests.models import CustomerRequest

    return CustomerRequest.reference_for(public_id)


def account_requests(account) -> list[RequestSummary]:
    """Every request this account owns, newest first, with its line snapshots."""
    if connection.vendor == "postgresql":
        _require_bound(account)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, public_id, human_number, status, preferred_messenger, created_at "
                "FROM customer_account_requests WHERE customer_account_id = %s "
                "ORDER BY created_at DESC, id DESC",
                [account.pk],
            )
            rows = cursor.fetchall()
            summaries = {
                row[0]: RequestSummary(
                    id=row[0],
                    public_id=UUID(str(row[1])),
                    number=_number(row[2], row[1]),
                    status=row[3],
                    messenger=row[4],
                    created_at=row[5],
                )
                for row in rows
            }
            if summaries:
                cursor.execute(
                    "SELECT request_id, article, part_name, quantity_requested, "
                    "unit_short_name, price_seen, is_supply_inquiry "
                    "FROM customer_account_request_lines WHERE request_id = ANY(%s) ORDER BY id",
                    [list(summaries)],
                )
                for request_id, article, name, qty, unit, price, inquiry in cursor.fetchall():
                    summaries[request_id].lines.append(
                        RequestLine(article, name, qty, unit, price, inquiry)
                    )
        return [summaries[row[0]] for row in rows]

    from apps.customer_requests.models import CustomerRequest

    requests = CustomerRequest.objects.filter(customer_account=account).prefetch_related("lines")
    result = []
    for req in requests.order_by("-created_at", "-pk"):
        summary = RequestSummary(
            id=req.pk,
            public_id=req.public_id,
            number=req.reference,
            status=req.status,
            messenger=req.preferred_messenger,
            created_at=req.created_at,
        )
        for line in sorted(req.lines.all(), key=lambda item: item.pk):
            summary.lines.append(
                RequestLine(
                    line.article,
                    line.part_name,
                    line.quantity_requested,
                    line.unit_short_name,
                    line.price_seen,
                    line.is_supply_inquiry,
                )
            )
        result.append(summary)
    return result


def account_request(account, public_id) -> RequestSummary | None:
    """One owned request by its opaque id. Anything else is simply not found."""
    try:
        wanted = UUID(str(public_id))
    except (TypeError, ValueError, AttributeError):
        return None
    return next((r for r in account_requests(account) if r.public_id == wanted), None)


# --- Purchases ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PurchaseLine:
    part_type_id: int
    article: str
    name: str
    quantity: Decimal
    unit_price: Decimal
    total_price: Decimal


@dataclass
class Purchase:
    id: int
    number: str
    sold_at: datetime | None
    lines: list[PurchaseLine] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((line.total_price for line in self.lines), ZERO)


def _part_labels(part_ids) -> dict[int, tuple[str, str]]:
    """Article and name for display. Catalog data the public role may read."""
    from apps.catalog.models import PartType
    from apps.inventory.presentation import part_exact_number

    parts = PartType.objects.filter(pk__in=set(part_ids)).prefetch_related("numbers")
    return {part.pk: (part_exact_number(part, default=""), part.name) for part in parts}


def _purchases_from_rows(sales, lines) -> list[Purchase]:
    purchases = {
        sale_id: Purchase(id=sale_id, number=number, sold_at=sold_at)
        for sale_id, number, sold_at in sales
    }
    labels = _part_labels(line[1] for line in lines)
    for sale_id, part_id, qty, unit_price, total_price in lines:
        if sale_id not in purchases:
            continue
        article, name = labels.get(part_id, ("", "Позиция больше не доступна"))
        purchases[sale_id].lines.append(
            PurchaseLine(part_id, article, name, qty, unit_price, total_price)
        )
    return [purchases[sale_id] for sale_id, _number, _sold in sales]


def account_purchases(account) -> list[Purchase]:
    """Completed sales of the employee-linked client card, newest first."""
    if connection.vendor == "postgresql":
        _require_bound(account)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, number, sold_at FROM customer_account_sales "
                "ORDER BY sold_at DESC NULLS LAST, id DESC"
            )
            sales = cursor.fetchall()
            lines = []
            if sales:
                cursor.execute(
                    "SELECT sale_id, part_type_id, quantity, unit_price, total_price "
                    "FROM customer_account_sale_lines WHERE sale_id = ANY(%s) ORDER BY id",
                    [[row[0] for row in sales]],
                )
                lines = cursor.fetchall()
        return _purchases_from_rows(sales, lines)

    from apps.sales.models import Sale, SaleLine

    from .services import linked_customer_id

    customer_id = linked_customer_id(account)
    if customer_id is None:
        return []
    sales = list(
        Sale.objects.filter(customer_id=customer_id, status=Sale.Status.COMPLETED)
        .order_by("-sold_at", "-pk")
        .values_list("id", "number", "sold_at")
    )
    lines = list(
        SaleLine.objects.filter(sale_id__in=[row[0] for row in sales])
        .order_by("pk")
        .values_list("sale_id", "part_type_id", "quantity", "unit_price", "total_price")
    )
    return _purchases_from_rows(sales, lines)


def account_purchase(account, number: str) -> Purchase | None:
    """One owned purchase. A sale of anyone else is 'not found', same as none."""
    number = str(number or "")[:20]
    return next((p for p in account_purchases(account) if p.number == number), None)
