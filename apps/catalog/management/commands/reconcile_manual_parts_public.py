"""Audit or explicitly reconcile the normal manual-part publication state."""

from django.core.management.base import BaseCommand

from apps.catalog.models import PartType
from apps.catalog.services import MANUAL_CATEGORY_NAME


class Command(BaseCommand):
    help = (
        "Audit manually created sellable parts; with --apply classify their "
        "positive entered prices through the canonical public resolver."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply only the price-provenance reconciliation; hidden parts stay hidden.",
        )

    def handle(self, *args, **options):
        manual = PartType.objects.filter(category__name=MANUAL_CATEGORY_NAME)
        candidates = manual.filter(
            is_active=True,
            recommended_price__gt=0,
            brp_link__isnull=True,
            polaris_link__isnull=True,
            aftermarket_catalog_entry__isnull=True,
            arctic_cat_catalog_entry__isnull=True,
        ).exclude(price_provenance=PartType.PriceProvenance.VALID_MANUAL_EXCEPTION)
        hidden = candidates.filter(is_public=False).count()
        public = candidates.filter(is_public=True).count()

        self.stdout.write(f"Категория: {MANUAL_CATEGORY_NAME}")
        self.stdout.write(f"Кандидатов с положительной ручной ценой: {candidates.count()}")
        self.stdout.write(f"Из них уже публичных: {public}")
        self.stdout.write(f"Из них намеренно скрытых: {hidden}")

        if not options["apply"]:
            self.stdout.write("Режим проверки: изменений нет. Для применения добавьте --apply.")
            return

        updated = candidates.update(
            certified_price_rub=None,
            price_provenance=PartType.PriceProvenance.VALID_MANUAL_EXCEPTION,
        )
        self.stdout.write(f"Обновлено ценовых оснований: {updated}")
        if hidden:
            self.stdout.write("Скрытые детали не опубликованы: явный is_public=False сохранён.")
