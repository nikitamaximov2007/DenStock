"""Отчёт о возможной привязке исторических документов к карточкам клиентов.

ТОЛЬКО ЧТЕНИЕ. Команда ничего не связывает и ничего не пишет: она показывает,
что можно было бы связать, и на каком основании.

Почему нет автоматической привязки: одинаковое имя не означает одного человека
(тёзки), а одинаковый телефон тоже не означает одного человека (семейный,
рабочий или общий номер организации). Решение «это тот же клиент» может принять
только сотрудник, поэтому команда выдаёт кандидатов, а не выполняет слияние.

    python manage.py audit_customer_links
    python manage.py audit_customer_links --limit 50
"""

from django.core.management.base import BaseCommand

from apps.core.phones import normalize_phone
from apps.customers.models import Customer
from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, Sale

DOCUMENTS = (
    ("Продажа", Sale),
    ("Ремонт", RepairOrder),
    ("Резерв", Reservation),
)


class Command(BaseCommand):
    help = "Dry-run отчёт: какие документы без карточки клиента можно связать вручную."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20, help="Сколько примеров показать.")

    def handle(self, *args, **options):
        limit = max(1, options["limit"])
        customers = list(Customer.objects.all())
        by_name: dict[str, list] = {}
        by_phone: dict[str, list] = {}
        for customer in customers:
            by_name.setdefault(customer.name.strip().casefold(), []).append(customer)
            if customer.phone_normalized:
                by_phone.setdefault(customer.phone_normalized, []).append(customer)

        self.stdout.write("РЕЖИМ: только чтение. Ни один документ не изменён.")
        self.stdout.write(f"Карточек клиентов: {len(customers)}")

        for label, model in DOCUMENTS:
            unlinked = model.objects.filter(customer__isnull=True)
            total = unlinked.count()
            exact_one = 0
            ambiguous = 0
            no_match = 0
            examples = []
            for document in unlinked.only(
                "pk", "number", "customer_name", "customer_phone"
            ).iterator():
                name_key = (document.customer_name or "").strip().casefold()
                phone_key = normalize_phone(document.customer_phone)
                candidates = {c.pk: c for c in by_name.get(name_key, [])}
                if phone_key:
                    for candidate in by_phone.get(phone_key, []):
                        candidates[candidate.pk] = candidate
                if not candidates:
                    no_match += 1
                elif len(candidates) == 1:
                    exact_one += 1
                    if len(examples) < limit:
                        only = next(iter(candidates.values()))
                        examples.append(
                            f"  {document.number}: «{document.customer_name}» "
                            f"-> карточка #{only.pk} «{only.name}»"
                        )
                else:
                    ambiguous += 1

            self.stdout.write("")
            self.stdout.write(f"{label}: без карточки {total}")
            self.stdout.write(f"  ровно один кандидат: {exact_one}")
            self.stdout.write(f"  несколько кандидатов (связывать нельзя): {ambiguous}")
            self.stdout.write(f"  кандидатов нет: {no_match}")
            for line in examples:
                self.stdout.write(line)

        self.stdout.write("")
        self.stdout.write(
            "Даже строка «ровно один кандидат» это ПРЕДПОЛОЖЕНИЕ, а не факт: "
            "тёзки и общие номера встречаются. Связывать документы нужно вручную."
        )
