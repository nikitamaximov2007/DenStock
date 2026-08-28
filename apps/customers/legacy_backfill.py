"""Явный, консервативный backfill исторических клиентов.

Это не используется ни одной runtime-формой.  Связь допускается только для
однозначной пары сохранённых имени и телефона; совпадение только имени или
только телефона намеренно остаётся для ручной проверки.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass

from django.db import transaction

from apps.core.phones import normalize_phone
from apps.customers.models import Customer
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale


@dataclass(frozen=True)
class LegacyDocument:
    kind: str
    pk: int
    number: str
    name: str
    phone: str


def _name_key(value: str) -> str:
    return (value or "").strip().casefold()


def _documents():
    for kind, model in (("sales", Sale), ("repairs", RepairOrder)):
        for document in model.objects.filter(
            customer__isnull=True, status=model.Status.COMPLETED
        ).only("pk", "number", "customer_name", "customer_phone"):
            yield LegacyDocument(
                kind, document.pk, document.number, document.customer_name, document.customer_phone
            )


def plan_legacy_customer_backfill():
    """Return an auditable plan without writing anything."""
    documents = list(_documents())
    eligible = [
        document for document in documents
        if _name_key(document.name) and normalize_phone(document.phone)
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
        planned.append({"name": group[0].name.strip(), "phone": group[0].phone.strip(),
                        "customer": matches[0] if matches else None, "documents": group})

    return {
        "documents_scanned": len(documents), "legacy_identities": len(groups),
        "skipped_documents": skipped, "ambiguous": ambiguous, "planned": planned,
    }


def apply_legacy_customer_backfill():
    """Apply a fresh plan atomically; never alter document snapshots."""
    with transaction.atomic():
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
        return {**plan, "created": created, "reused": reused, "sales_linked": sales,
                "repairs_linked": repairs}
