"""Read-only audit of phone-based request/customer matches."""
from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.core.phones import normalize_phone
from apps.customers.models import Customer

from ...models import CustomerRequest
from ...sale_conversion import match_request_customer


def _redacted_pk(value: int) -> str:
    return f"…{str(value)[-4:]}"


class Command(BaseCommand):
    help = "Проверить совпадения заявок и клиентов по нормализованному телефону (только чтение)."

    def handle(self, *args, **options):
        groups = defaultdict(list)
        customers_with_phones = 0
        valid_normalized = 0
        for customer in Customer.objects.only("pk", "phone"):
            if not customer.phone.strip():
                continue
            customers_with_phones += 1
            normalized = normalize_phone(customer.phone)
            if normalized:
                valid_normalized += 1
                groups[normalized].append(customer.pk)

        duplicate_groups = {phone: ids for phone, ids in groups.items() if len(ids) > 1}
        request_counts = {"one": 0, "zero": 0, "multiple": 0}
        for request in CustomerRequest.objects.only("pk", "customer_phone"):
            count = match_request_customer(request).count
            if count == 1:
                request_counts["one"] += 1
            elif count == 0:
                request_counts["zero"] += 1
            else:
                request_counts["multiple"] += 1

        self.stdout.write(f"Клиенты с телефоном: {customers_with_phones}")
        self.stdout.write(f"Клиенты с валидным нормализованным телефоном: {valid_normalized}")
        self.stdout.write(f"Группы клиентов с дубликатами телефона: {len(duplicate_groups)}")
        self.stdout.write(f"Заявки с одним совпадением: {request_counts['one']}")
        self.stdout.write(f"Заявки без совпадения: {request_counts['zero']}")
        self.stdout.write(f"Заявки с несколькими совпадениями: {request_counts['multiple']}")
        for index, ids in enumerate(sorted(duplicate_groups.values()), start=1):
            redacted = ", ".join(_redacted_pk(pk) for pk in ids)
            self.stdout.write(f"Дубликат {index}: клиенты {redacted}")
