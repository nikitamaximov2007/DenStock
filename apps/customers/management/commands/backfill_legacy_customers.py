"""Explicit, audited legacy customer backfill; dry-run by default."""

from django.core.management.base import BaseCommand

from apps.customers.legacy_backfill import (
    apply_legacy_customer_backfill,
    plan_legacy_customer_backfill,
)


class Command(BaseCommand):
    help = "Консервативно связать completed legacy-документы с карточками клиентов."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Выполнить изменения (по умолчанию dry-run)."
        )

    def handle(self, *args, **options):
        result = (
            apply_legacy_customer_backfill()
            if options["apply"]
            else plan_legacy_customer_backfill()
        )
        mode = "APPLY" if options["apply"] else "DRY RUN"
        self.stdout.write(f"РЕЖИМ: {mode}")
        self.stdout.write(f"Документов просмотрено: {result['documents_scanned']}")
        self.stdout.write(f"Legacy identities: {result['legacy_identities']}")
        self.stdout.write(f"Безопасных групп: {len(result['planned'])}")
        self.stdout.write(f"Пропущено документов: {result['skipped_documents']}")
        self.stdout.write(f"Неоднозначных групп: {sum(result['ambiguous'].values())}")
        for reason, count in sorted(result["ambiguous"].items()):
            self.stdout.write(f"  {reason}: {count}")
        if options["apply"]:
            self.stdout.write(
                "Создано карточек: "
                f"{result['created']}; использовано существующих: {result['reused']}"
            )
            self.stdout.write(
                f"Связано продаж: {result['sales_linked']}; ремонтов: {result['repairs_linked']}"
            )
