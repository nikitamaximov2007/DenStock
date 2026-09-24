"""Read-only audit of duplicate/invalid Customer phone identity.

Bulk queries only, no N+1: one GROUP BY to find duplicate phones, one
IN-filtered fetch of the candidate rows, and one aggregated count query per
related model (Sale/RepairOrder/CustomerRequest) - never a query per
Customer. See ``audit_customer_phone_duplicates`` for the exact query count.

Phone identity is strong evidence; name similarity is only a review hint and
never decides a classification by itself (see the task's explicit rule).
Reuses ``apps.core.phones.normalize_phone`` - the same helper
``Customer.phone_normalized`` and the CustomerRequest -> Sale match already
use. No second normalization algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count

from .models import Customer

# A normalized phone shorter than this cannot plausibly be a real number
# (the shortest legitimate case this helper produces is a 10-digit local
# Russian mobile normalized to 11 digits, or a foreign number left as-is).
# This is a review heuristic, not a rule borrowed from normalize_phone
# itself: the helper never rejects input, it only extracts digits.
MIN_PLAUSIBLE_DIGITS = 7

SAFE_LIKELY_DUPLICATE = "safe_likely_duplicate"
REVIEW_REQUIRED = "review_required"


def _name_key(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


def _names_compatible(names: list[str]) -> bool:
    """True when every name is the same, or one is a prefix of another.

    Handles "Александр Пушкарёв" vs "Александр" (partial data entry) as
    compatible, but "Александр" vs "Мария" as a conflict. This is a
    conservative review hint, not proof of identity - see module docstring.
    """
    keys = {_name_key(name) for name in names if _name_key(name)}
    if len(keys) <= 1:
        return True
    ordered = sorted(keys, key=len)
    shortest = ordered[0]
    return all(key == shortest or key.startswith(shortest + " ") for key in ordered[1:])


@dataclass(frozen=True)
class DuplicateCustomerRow:
    """One Customer row inside a duplicate-phone group."""

    customer_id: int
    name: str
    sale_count: int
    repair_count: int
    reservation_count: int
    request_count: int


@dataclass(frozen=True)
class DuplicateGroupRow:
    normalized_phone: str
    customers: tuple[DuplicateCustomerRow, ...]
    classification: str
    reason: str

    @property
    def customer_ids(self) -> tuple[int, ...]:
        return tuple(row.customer_id for row in self.customers)

    @property
    def has_sales(self) -> bool:
        return any(row.sale_count for row in self.customers)

    @property
    def has_repairs(self) -> bool:
        return any(row.repair_count for row in self.customers)

    @property
    def has_requests(self) -> bool:
        return any(row.request_count for row in self.customers)


@dataclass(frozen=True)
class NameOnlyGroupRow:
    """Customers sharing a name with NO phone evidence at all - a hint only,
    never used to justify a merge by itself (task: name similarity is only a
    review hint)."""

    name_key: str
    customer_ids: tuple[int, ...]


@dataclass(frozen=True)
class AuditReport:
    total_customers: int
    with_phone: int
    valid_normalized_phone: int
    missing_phone: int
    invalid_phone: int
    duplicate_groups: int
    customers_in_duplicate_groups: int
    duplicate_groups_with_sales: int
    duplicate_groups_with_repairs: int
    duplicate_groups_with_both: int
    duplicate_groups_referenced_by_request: int
    groups_with_conflicting_names: int
    groups: list = field(default_factory=list)
    name_only_groups: list = field(default_factory=list)


def audit_customer_phone_duplicates() -> AuditReport:
    """Classify every Customer by phone identity. Read-only, bulk queries only.

    Query count is fixed regardless of table size: one aggregate count query,
    one GROUP BY for duplicate phones, one fetch of the candidate rows, three
    aggregated per-model relation counts (Sale/RepairOrder/CustomerRequest),
    and one query for the name-only hint group. No query is issued per
    Customer or per group.
    """
    from apps.customer_requests.models import CustomerRequest
    from apps.repairs.models import RepairOrder
    from apps.sales.models import Reservation, Sale

    total_customers = Customer.objects.count()
    with_phone = Customer.objects.exclude(phone="").count()
    missing_phone = total_customers - with_phone

    # phone_normalized == "" only happens when `phone` has no digits at all
    # (see apps.core.phones.normalize_phone) - i.e. text with no digits.
    invalid_qs = Customer.objects.exclude(phone="").filter(phone_normalized="")
    invalid_phone = invalid_qs.count()
    valid_normalized_phone = with_phone - invalid_phone

    dup_phones = list(
        Customer.objects.exclude(phone_normalized="")
        .values("phone_normalized")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("phone_normalized", flat=True)
    )

    groups: list[DuplicateGroupRow] = []
    customers_in_duplicate_groups = 0
    duplicate_groups_with_sales = 0
    duplicate_groups_with_repairs = 0
    duplicate_groups_with_both = 0
    duplicate_groups_referenced_by_request = 0
    groups_with_conflicting_names = 0

    if dup_phones:
        candidates = list(
            Customer.objects.filter(phone_normalized__in=dup_phones)
            .order_by("phone_normalized", "pk")
            .values("id", "name", "phone_normalized")
        )
        candidate_ids = [row["id"] for row in candidates]
        sale_counts = _counts_by_customer(Sale.objects.filter(customer_id__in=candidate_ids))
        repair_counts = _counts_by_customer(
            RepairOrder.objects.filter(customer_id__in=candidate_ids)
        )
        reservation_counts = _counts_by_customer(
            Reservation.objects.filter(customer_id__in=candidate_ids)
        )
        request_counts = _counts_by_customer(
            CustomerRequest.objects.filter(customer_id__in=candidate_ids)
        )

        by_phone: dict[str, list[dict]] = {}
        for row in candidates:
            by_phone.setdefault(row["phone_normalized"], []).append(row)

        for phone, rows in by_phone.items():
            customer_rows = tuple(
                DuplicateCustomerRow(
                    customer_id=row["id"],
                    name=row["name"],
                    sale_count=sale_counts.get(row["id"], 0),
                    repair_count=repair_counts.get(row["id"], 0),
                    reservation_count=reservation_counts.get(row["id"], 0),
                    request_count=request_counts.get(row["id"], 0),
                )
                for row in rows
            )
            names = [row["name"] for row in rows]
            compatible = _names_compatible(names)
            if compatible:
                classification = SAFE_LIKELY_DUPLICATE
                reason = "тот же канонический телефон, совместимые имена"
            else:
                classification = REVIEW_REQUIRED
                reason = "тот же телефон, но имена существенно различаются"
                groups_with_conflicting_names += 1

            group = DuplicateGroupRow(
                normalized_phone=phone, customers=customer_rows,
                classification=classification, reason=reason,
            )
            groups.append(group)
            customers_in_duplicate_groups += len(customer_rows)
            if group.has_sales:
                duplicate_groups_with_sales += 1
            if group.has_repairs:
                duplicate_groups_with_repairs += 1
            if group.has_sales and group.has_repairs:
                duplicate_groups_with_both += 1
            if group.has_requests:
                duplicate_groups_referenced_by_request += 1

    name_only_groups = _name_only_groups()

    return AuditReport(
        total_customers=total_customers,
        with_phone=with_phone,
        valid_normalized_phone=valid_normalized_phone,
        missing_phone=missing_phone,
        invalid_phone=invalid_phone,
        duplicate_groups=len(groups),
        customers_in_duplicate_groups=customers_in_duplicate_groups,
        duplicate_groups_with_sales=duplicate_groups_with_sales,
        duplicate_groups_with_repairs=duplicate_groups_with_repairs,
        duplicate_groups_with_both=duplicate_groups_with_both,
        duplicate_groups_referenced_by_request=duplicate_groups_referenced_by_request,
        groups_with_conflicting_names=groups_with_conflicting_names,
        groups=sorted(groups, key=lambda g: g.normalized_phone),
        name_only_groups=name_only_groups,
    )


def _counts_by_customer(queryset) -> dict[int, int]:
    return dict(
        queryset.values_list("customer_id")
        .annotate(n=Count("id"))
        .values_list("customer_id", "n")
    )


def _name_only_groups() -> list[NameOnlyGroupRow]:
    """Customers with NO phone at all sharing the same normalized name.

    A hint only (category E): never mixed with the phone-evidence groups
    above, never usable to justify a merge by itself.
    """
    missing_phone = list(
        Customer.objects.filter(phone="").exclude(name="").values("id", "name")
    )
    by_name: dict[str, list[int]] = {}
    for row in missing_phone:
        key = _name_key(row["name"])
        if key:
            by_name.setdefault(key, []).append(row["id"])
    return [
        NameOnlyGroupRow(name_key=key, customer_ids=tuple(sorted(ids)))
        for key, ids in sorted(by_name.items())
        if len(ids) > 1
    ]


def duplicate_group_for_phone(normalized_phone: str, *, exclude_id: int | None = None):
    """Cheap, targeted lookup for one phone - for the customer-detail banner (§13).

    Deliberately NOT a call into the full audit: that scans every duplicate
    phone in the table, which is fine for an occasional management command
    but wrong for a page every operator opens constantly. This is a handful
    of small, indexed queries scoped to exactly one normalized phone.
    """
    if not normalized_phone:
        return None
    from apps.customer_requests.models import CustomerRequest
    from apps.repairs.models import RepairOrder
    from apps.sales.models import Reservation, Sale

    rows = list(
        Customer.objects.filter(phone_normalized=normalized_phone, merged_into__isnull=True)
        .exclude(pk=exclude_id)
        .order_by("pk")
        .values("id", "name")
    )
    if not rows:
        return None
    ids = [row["id"] for row in rows]
    sale_counts = _counts_by_customer(Sale.objects.filter(customer_id__in=ids))
    repair_counts = _counts_by_customer(RepairOrder.objects.filter(customer_id__in=ids))
    reservation_counts = _counts_by_customer(Reservation.objects.filter(customer_id__in=ids))
    request_counts = _counts_by_customer(CustomerRequest.objects.filter(customer_id__in=ids))
    customer_rows = tuple(
        DuplicateCustomerRow(
            customer_id=row["id"], name=row["name"],
            sale_count=sale_counts.get(row["id"], 0),
            repair_count=repair_counts.get(row["id"], 0),
            reservation_count=reservation_counts.get(row["id"], 0),
            request_count=request_counts.get(row["id"], 0),
        )
        for row in rows
    )
    names = [row["name"] for row in rows]
    compatible = _names_compatible(names)
    classification = SAFE_LIKELY_DUPLICATE if compatible else REVIEW_REQUIRED
    reason = (
        "тот же канонический телефон, совместимые имена" if compatible
        else "тот же телефон, но имена существенно различаются"
    )
    return DuplicateGroupRow(
        normalized_phone=normalized_phone, customers=customer_rows,
        classification=classification, reason=reason,
    )
