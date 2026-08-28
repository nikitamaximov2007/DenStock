"""Явный, консервативный backfill исторических клиентов.

Это не используется ни одной runtime-формой.  Связь допускается только для
однозначной пары сохранённых имени и телефона; совпадение только имени или
только телефона намеренно остаётся для ручной проверки.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from django.db import connections, transaction

from apps.core.phones import normalize_phone
from apps.customers.models import Customer, CustomerPeriodPaymentAcknowledgement
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale

# A transaction-scoped PostgreSQL lock serializes explicit operator runs.  A
# Customer identity intentionally has no database uniqueness constraint (the
# same name or phone can belong to different people), so row locks alone cannot
# protect creation of a previously absent safe identity.
CUSTOMER_BACKFILL_ADVISORY_LOCK_ID = 5_476_321_986_421


@dataclass(frozen=True)
class LegacyDocument:
    kind: str
    pk: int
    number: str
    name: str
    phone: str
    occurred_on: date | None


def _name_key(value: str) -> str:
    return (value or "").strip().casefold()


def _documents():
    for kind, model, occurred_field in (
        ("sales", Sale, "sold_at"),
        ("repairs", RepairOrder, "completed_at"),
    ):
        for document in model.objects.filter(
            customer__isnull=True, status=model.Status.COMPLETED
        ).only("pk", "number", "customer_name", "customer_phone", occurred_field):
            occurred_at = getattr(document, occurred_field)
            yield LegacyDocument(
                kind,
                document.pk,
                document.number,
                document.customer_name,
                document.customer_phone,
                occurred_at.date() if occurred_at else None,
            )


def _lock_backfill_run() -> None:
    """Serialize apply runs without imposing identity uniqueness on Customer."""
    connection = connections["default"]
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [CUSTOMER_BACKFILL_ADVISORY_LOCK_ID])


def _has_active_acknowledgement_in_document_period(*, acknowledgements, documents) -> bool:
    """Do not make a prior payment acknowledgement stale through backfill."""
    for document in documents:
        for acknowledgement in acknowledgements:
            if document.occurred_on is None:
                return True
            if acknowledgement.period_start <= document.occurred_on <= acknowledgement.period_end:
                return True
    return False


def plan_legacy_customer_backfill():
    """Return an auditable plan without writing anything."""
    documents = list(_documents())
    eligible = [
        document
        for document in documents
        if _name_key(document.name) and normalize_phone(document.phone)
    ]
    name_only_documents = [
        document
        for document in documents
        if _name_key(document.name) and not normalize_phone(document.phone)
    ]
    phone_only_documents = [
        document
        for document in documents
        if not _name_key(document.name) and normalize_phone(document.phone)
    ]
    missing_identity_documents = [
        document
        for document in documents
        if not _name_key(document.name) and not normalize_phone(document.phone)
    ]
    name_phones = defaultdict(set)
    phone_names = defaultdict(set)
    groups = defaultdict(list)
    for document in eligible:
        name, phone = _name_key(document.name), normalize_phone(document.phone)
        name_phones[name].add(phone)
        phone_names[phone].add(name)
        groups[(name, phone)].append(document)

    customers = defaultdict(list)
    for customer in Customer.objects.only("pk", "name", "phone", "phone_normalized"):
        customers[(_name_key(customer.name), customer.phone_normalized)].append(customer)
    acknowledgements = defaultdict(list)
    for acknowledgement in CustomerPeriodPaymentAcknowledgement.objects.filter(
        revoked_at__isnull=True
    ).only("customer_id", "period_start", "period_end"):
        acknowledgements[acknowledgement.customer_id].append(acknowledgement)

    planned = []
    ambiguous = Counter()
    skipped = 0
    for document in documents:
        if not _name_key(document.name) or not normalize_phone(document.phone):
            skipped += 1
    for (name, phone), group in groups.items():
        if len(name_phones[name]) != 1:
            ambiguous["name_multiple_phones"] += 1
            continue
        if len(phone_names[phone]) != 1:
            ambiguous["phone_multiple_names"] += 1
            continue
        matches = customers[(name, phone)]
        if len(matches) > 1:
            ambiguous["multiple_existing_customers"] += 1
            continue
        if matches and _has_active_acknowledgement_in_document_period(
            acknowledgements=acknowledgements[matches[0].pk], documents=group
        ):
            ambiguous["active_payment_acknowledgement"] += 1
            continue
        planned.append(
            {
                "name": group[0].name.strip(),
                "phone": group[0].phone.strip(),
                "customer": matches[0] if matches else None,
                "documents": group,
            }
        )

    return {
        "documents_scanned": len(documents),
        "completed_legacy_sales": sum(document.kind == "sales" for document in documents),
        "completed_legacy_repairs": sum(document.kind == "repairs" for document in documents),
        "legacy_identities": len(groups),
        "name_phone_documents": len(eligible),
        "name_only_documents": len(name_only_documents),
        "phone_only_documents": len(phone_only_documents),
        "missing_identity_documents": len(missing_identity_documents),
        "skipped_documents": skipped,
        "ambiguous": ambiguous,
        "planned": planned,
        "existing_customers_reused": sum(group["customer"] is not None for group in planned),
        "new_customers_proposed": sum(group["customer"] is None for group in planned),
        "sales_proposed": sum(
            document.kind == "sales" for group in planned for document in group["documents"]
        ),
        "repairs_proposed": sum(
            document.kind == "repairs" for group in planned for document in group["documents"]
        ),
    }


def apply_legacy_customer_backfill():
    """Apply a fresh plan atomically; never alter document snapshots."""
    with transaction.atomic():
        _lock_backfill_run()
        plan = plan_legacy_customer_backfill()
        created = reused = sales = repairs = 0
        for group in plan["planned"]:
            customer = group["customer"]
            if customer is None:
                customer = Customer.objects.create(name=group["name"], phone=group["phone"])
                created += 1
            else:
                reused += 1
            sale_ids = [doc.pk for doc in group["documents"] if doc.kind == "sales"]
            repair_ids = [doc.pk for doc in group["documents"] if doc.kind == "repairs"]
            # The null guard makes a repeated apply idempotent and protects a
            # concurrent/manual link.  Only the FK changes; snapshots do not.
            sales += Sale.objects.filter(pk__in=sale_ids, customer__isnull=True).update(
                customer=customer
            )
            repairs += RepairOrder.objects.filter(pk__in=repair_ids, customer__isnull=True).update(
                customer=customer
            )
        return {
            **plan,
            "created": created,
            "reused": reused,
            "sales_linked": sales,
            "repairs_linked": repairs,
        }
