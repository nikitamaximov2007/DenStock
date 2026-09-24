"""Safe, explicit, one-at-a-time Customer merge.

Never auto-selects a canonical target, never iterates duplicate groups on
its own, never deletes or rewrites the source's history. The operator picks
target and source explicitly (see apps.customers.dedup_audit for finding
candidates); this module only knows how to move one pair safely.

What moves: every PROTECT-guarded cross-app relation that points at
`customer` (Sale, Reservation, RepairOrder, CustomerRequest, OrderedPart,
CustomerAccountCustomerLink, CustomerPeriodPaymentAcknowledgement) is
reassigned to the target via a bulk ``.update(customer=target)`` - never a
per-row save, and never touching the frozen customer_name/customer_phone
snapshot fields those documents already carry. Money, prices, stock and
dates are never touched: only the *current identity reference* moves.

What never moves: `CustomerCreateIdempotency` stays with the source - it is
evidence of a specific historical creation event, not a business record that
should follow the merged identity.

Source lifecycle: the source row is never deleted. It is tombstoned via
`Customer.merged_into` pointing at the target, which normal customer-
selection queries (`search_customers`, `customers_by_recent_activity`)
exclude, so staff can't pick a merged card for a new document by accident,
while its detail page and every historical document it's still credited
with (documents that existed before the merge, if any survive on it - see
below) remain directly viewable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import connections, transaction

from .models import Customer, CustomerMergeReceipt

# Distinct from apps.customers.legacy_backfill.CUSTOMER_BACKFILL_ADVISORY_LOCK_ID
# (5_476_321_986_421) - a different operation must not share its lock name.
MERGE_ADVISORY_LOCK_ID = 5_476_321_986_422

RELATION_LABELS = {
    "sales": "продаж",
    "reservations": "резервов",
    "repairs": "ремонтов",
    "customer_requests": "заявок",
    "ordered_parts": "заказанных деталей",
    "customer_account_links": "связей кабинетов",
    "payment_acknowledgements": "подтверждений оплаты",
}


class CustomerMergeError(Exception):
    """Merge cannot proceed safely."""


@dataclass(frozen=True)
class MergePlan:
    """Read-only preview: what WOULD move if this merge were applied."""

    target: Customer
    source: Customer
    moved_counts: dict = field(default_factory=dict)
    already_merged_here: bool = False

    @property
    def total_moved(self) -> int:
        return sum(self.moved_counts.values())


def _lock_merge_run() -> None:
    """Serialize merge runs without imposing identity uniqueness on Customer.

    Same technique as apps.customers.legacy_backfill._lock_backfill_run.
    """
    connection = connections["default"]
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [MERGE_ADVISORY_LOCK_ID])


def _relation_querysets(customer):
    from apps.customer_accounts.models import CustomerAccountCustomerLink
    from apps.customer_requests.models import CustomerRequest
    from apps.ordered_parts.models import OrderedPart
    from apps.repairs.models import RepairOrder
    from apps.sales.models import Reservation, Sale

    from .models import CustomerPeriodPaymentAcknowledgement

    return {
        "sales": Sale.objects.filter(customer=customer),
        "reservations": Reservation.objects.filter(customer=customer),
        "repairs": RepairOrder.objects.filter(customer=customer),
        "customer_requests": CustomerRequest.objects.filter(customer=customer),
        "ordered_parts": OrderedPart.objects.filter(customer=customer),
        "customer_account_links": CustomerAccountCustomerLink.objects.filter(customer=customer),
        "payment_acknowledgements": CustomerPeriodPaymentAcknowledgement.objects.filter(
            customer=customer
        ),
    }


def _relation_counts(customer) -> dict:
    return {key: qs.count() for key, qs in _relation_querysets(customer).items()}


def _ensure_distinct(target_id: int, source_id: int) -> None:
    if target_id == source_id:
        raise CustomerMergeError("Целевая и исходная карточка не могут быть одной и той же.")


def preview_customer_merge(target: Customer, source: Customer) -> MergePlan:
    """Read-only: never writes. Safe to call at any time, including in a view."""
    _ensure_distinct(target.pk, source.pk)
    if source.merged_into_id is not None:
        return MergePlan(
            target=target, source=source, moved_counts={},
            already_merged_here=source.merged_into_id == target.pk,
        )
    if target.merged_into_id is not None:
        raise CustomerMergeError(
            f"Целевая карточка #{target.pk} сама уже объединена с "
            f"#{target.merged_into_id} - выберите живую каноническую карточку."
        )
    return MergePlan(target=target, source=source, moved_counts=_relation_counts(source))


@transaction.atomic
def execute_customer_merge(
    *, target_id: int, source_id: int, by=None, reason: str = ""
) -> CustomerMergeReceipt:
    """Apply the merge: reassign every relation, tombstone the source.

    Idempotent for the exact same (target, source) pair: calling it again
    once the source is already merged into this target returns the existing
    receipt without moving anything a second time. A source already merged
    into a DIFFERENT target fails closed - that contradiction is an
    operator's decision to resolve by hand, never silently overwritten.
    """
    _ensure_distinct(target_id, source_id)
    _lock_merge_run()
    # Lock both rows in a PK-stable order regardless of which one is target
    # vs source, so two concurrent merges referencing the same pair from
    # opposite directions can never deadlock against each other.
    locked = {
        row.pk: row
        for row in Customer.objects.select_for_update().filter(
            pk__in=sorted([target_id, source_id])
        )
    }
    if target_id not in locked or source_id not in locked:
        raise CustomerMergeError("Целевая или исходная карточка не найдена.")
    target = locked[target_id]
    source = locked[source_id]

    if source.merged_into_id is not None:
        if source.merged_into_id != target.pk:
            raise CustomerMergeError(
                f"Источник #{source.pk} уже объединён с другой карточкой "
                f"(#{source.merged_into_id}), а не с #{target.pk}."
            )
        existing = (
            CustomerMergeReceipt.objects.filter(source=source, target=target)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing
        # Tombstoned but no surviving receipt (should not happen in normal
        # operation) - nothing left to move, but still record evidence.
        return CustomerMergeReceipt.objects.create(
            target=target, source=source, normalized_phone=source.phone_normalized,
            performed_by=by, moved_counts={}, reason=(reason or "").strip(),
        )
    if target.merged_into_id is not None:
        raise CustomerMergeError(
            f"Целевая карточка #{target.pk} сама уже объединена с "
            f"#{target.merged_into_id} - выберите живую каноническую карточку."
        )

    moved_counts = {
        key: qs.update(customer=target) for key, qs in _relation_querysets(source).items()
    }
    source.merged_into = target
    source.save(update_fields=["merged_into", "updated_at"])

    return CustomerMergeReceipt.objects.create(
        target=target, source=source, normalized_phone=source.phone_normalized,
        performed_by=by, moved_counts=moved_counts, reason=(reason or "").strip(),
    )
