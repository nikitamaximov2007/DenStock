"""Audit that PRO-STOR mirrors DenisStock's current customer price."""

from django.core.management.base import BaseCommand

from apps.catalog.public_catalog import public_parts
from apps.catalog.public_contracts import (
    PRICE_PARITY_CATEGORIES,
    audit_public_price_parity,
)


class Command(BaseCommand):
    help = "Сверить текущую цену PRO-STOR с ценой DenisStock. Ничего не изменяет."

    def add_arguments(self, parser):
        parser.add_argument(
            "--show",
            type=int,
            default=25,
            help="Сколько несовпадений напечатать подробно (0 - не печатать).",
        )

    def handle(self, *args, **options):
        report = audit_public_price_parity(
            public_parts().only("id", "recommended_price")
        )
        self.stdout.write("Аудит паритета цен: только чтение, цены не изменены")
        self.stdout.write(f"Публичных/sellable деталей: {len(report.rows)}")
        for category in PRICE_PARITY_CATEGORIES:
            self.stdout.write(f"  {category}: {report.counts[category]}")

        mismatches = [row for row in report.rows if row.category not in ("A", "D")]
        if not mismatches:
            self.stdout.write(self.style.SUCCESS("Расхождений цены нет."))
            return

        limit = options["show"]
        if limit <= 0:
            return
        self.stdout.write(f"Несовпадения, первые {min(limit, len(mismatches))}:")
        for row in mismatches[:limit]:
            self.stdout.write(
                f"  #{row.part_id} [{row.category}] "
                f"DenisStock={row.internal_price!s} ₽, "
                f"PRO-STOR={row.public_price!s} ₽"
            )
