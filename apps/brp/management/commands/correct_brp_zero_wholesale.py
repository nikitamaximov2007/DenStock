"""Create an auditable correction for a BRP snapshot that erased known prices."""
from django.core.management.base import BaseCommand, CommandError

from apps.brp.correction import (
    BrpPriceCorrectionError,
    apply_zero_wholesale_correction,
    plan_zero_wholesale_correction,
)
from apps.catalog_import.models import CatalogImportBatch


class Command(BaseCommand):
    help = "Исправить нулевые BRP wholesale по предыдущему официальному прайсу."

    def add_arguments(self, parser):
        parser.add_argument("--source-batch", type=int, required=True)
        parser.add_argument("--previous-file", required=True)
        parser.add_argument("--commit", action="store_true")
        parser.add_argument(
            "--confirm",
            default="",
            help="Для записи введите: CORRECT BRP ZERO WHOLESALE",
        )

    def handle(self, *args, **options):
        try:
            source = CatalogImportBatch.objects.get(pk=options["source_batch"])
        except CatalogImportBatch.DoesNotExist as exc:
            raise CommandError("Исходная партия не найдена.") from exc
        if options["commit"] and options["confirm"] != "CORRECT BRP ZERO WHOLESALE":
            raise CommandError("Для записи требуется точное подтверждение.")
        try:
            if options["commit"]:
                correction, summary = apply_zero_wholesale_correction(
                    source, previous_file=options["previous_file"]
                )
                self.stdout.write(
                    self.style.SUCCESS(f"Создана correction-партия #{correction.pk}.")
                )
            else:
                summary, _selected = plan_zero_wholesale_correction(
                    source, previous_file=options["previous_file"]
                )
        except BrpPriceCorrectionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Режим: {'ЗАПИСАНО' if options['commit'] else 'DRY-RUN'}")
        self.stdout.write(f"К исправлению: {summary.rows_to_update}")
        self.stdout.write(f"Сохранено из предыдущего каталога: {summary.previous_catalog_fallback}")
        self.stdout.write(f"Цена отсутствует во всех источниках: {summary.no_usable_price}")
        self.stdout.write(f"Разных ненулевых цен в дубликатах: {summary.ambiguous_nonzero}")
        self.stdout.write(f"Обновлено рекомендованных цен: {summary.linked_prices_refreshed}")
