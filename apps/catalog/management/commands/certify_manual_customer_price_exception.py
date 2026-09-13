"""Explicit owner-approved certification for one manual customer price."""

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import PartNumber
from apps.catalog.services import certify_valid_manual_price_exception


class Command(BaseCommand):
    help = "Подтвердить одну ручную цену для публичного каталога (dry-run по умолчанию)."

    def add_arguments(self, parser):
        parser.add_argument("--part-number", required=True, help="Точный номер детали.")
        parser.add_argument(
            "--apply", action="store_true", help="Записать подтверждённое исключение."
        )

    def handle(self, *args, **options):
        number = options["part_number"].strip()
        matches = list(
            PartNumber.objects.select_related("part")
            .filter(value=number)
            .order_by("pk")[:2]
        )
        if len(matches) != 1:
            raise CommandError(
                f"Expected exactly one part for number {number!r}, found {len(matches)}."
            )
        part = matches[0].part
        if part.recommended_price is None or part.recommended_price <= 0:
            raise CommandError("A manual exception requires a positive current customer price.")
        self.stdout.write(
            f"Part {number}: {part.recommended_price} ₽; "
            f"current provenance: {part.price_provenance}"
        )
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Dry-run only. Use --apply after owner approval."))
            return
        certify_valid_manual_price_exception(part)
        self.stdout.write(self.style.SUCCESS("Manual price exception certified."))
