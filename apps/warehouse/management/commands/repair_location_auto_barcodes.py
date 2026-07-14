"""Repair automatic location barcodes left behind by earlier renames.

    python manage.py repair_location_auto_barcodes --dry-run
    python manage.py repair_location_auto_barcodes --apply
    python manage.py repair_location_auto_barcodes --location-id 24 --apply

Only a location whose current barcode exactly matches the old automatic barcode
from its latest rename history is eligible. Custom barcodes are never guessed.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from apps.warehouse.models import StorageLocation
from apps.warehouse.services import auto_location_barcode


class Command(BaseCommand):
    help = "Исправить legacy автоматические штрихкоды ячеек после переименования."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать исправления (без флага команда работает в dry-run).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Явно указать dry-run (поведение по умолчанию).",
        )
        parser.add_argument(
            "--location-id",
            type=int,
            help="Проверить и при --apply исправить только одну ячейку.",
        )

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("Выберите одно: --dry-run ИЛИ --apply.")
        apply = options["apply"]
        write = self.stdout.write
        mode = "APPLY" if apply else "DRY-RUN"
        write(f"Режим: {mode}")

        locations = StorageLocation.objects.order_by("pk")
        if options["location_id"] is not None:
            locations = locations.filter(pk=options["location_id"])

        changed = skipped = eligible = 0
        for location_id in locations.values_list("pk", flat=True):
            if apply:
                result = self._repair_one(location_id, write)
            else:
                result = self._preview_one(location_id, write)
            if result == "eligible":
                eligible += 1
            elif result == "changed":
                changed += 1
            elif result == "skipped":
                skipped += 1

        if apply:
            write(self.style.SUCCESS(f"Готово: исправлено {changed}, пропущено {skipped}."))
        else:
            write(f"DRY-RUN: будет исправлено {eligible}, пропущено {skipped}.")

    @staticmethod
    def _legacy_replacement(location: StorageLocation):
        history = location.rename_history.order_by("-renamed_at", "-pk").first()
        if history is None:
            return None
        old_barcode = auto_location_barcode(history.old_code)
        if location.code != history.new_code or location.barcode != old_barcode:
            return None
        return old_barcode, auto_location_barcode(location.code)

    def _preview_one(self, location_id: int, write) -> str:
        location = StorageLocation.objects.get(pk=location_id)
        replacement = self._legacy_replacement(location)
        if replacement is None:
            return "skipped"
        old_barcode, new_barcode = replacement
        if StorageLocation.objects.filter(barcode=new_barcode).exclude(pk=location.pk).exists():
            write(
                f"ПРОПУСК id={location.pk} code={location.code}: "
                f"{new_barcode} уже используется другой ячейкой."
            )
            return "skipped"
        write(
            f"БУДЕТ ИСПРАВЛЕНО id={location.pk} code={location.code}: "
            f"{old_barcode} -> {new_barcode}"
        )
        return "eligible"

    def _repair_one(self, location_id: int, write) -> str:
        with transaction.atomic():
            location = StorageLocation.objects.select_for_update().get(pk=location_id)
            replacement = self._legacy_replacement(location)
            if replacement is None:
                return "skipped"
            old_barcode, new_barcode = replacement
            if StorageLocation.objects.filter(barcode=new_barcode).exclude(pk=location.pk).exists():
                write(
                    f"ПРОПУСК id={location.pk} code={location.code}: "
                    f"{new_barcode} уже используется другой ячейкой."
                )
                return "skipped"
            try:
                with transaction.atomic():
                    location.barcode = new_barcode
                    location.save(update_fields=["barcode", "updated_at"])
            except IntegrityError:
                write(
                    f"ПРОПУСК id={location.pk} code={location.code}: "
                    f"{new_barcode} уже используется другой ячейкой."
                )
                return "skipped"
        write(
            f"ИСПРАВЛЕНО id={location.pk} code={location.code}: "
            f"{old_barcode} -> {new_barcode}"
        )
        return "changed"
