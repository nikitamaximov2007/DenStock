"""Safely restore preferred part cells from current stock and placement history."""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.inventory.models import PartPreferredLocation
from apps.inventory.preferred_locations import build_preferred_location_backfill


class Command(BaseCommand):
    help = (
        "Показывает или создаёт закреплённые ячейки деталей. По умолчанию только dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Создать только однозначные закрепления из подготовленного плана.",
        )
        parser.add_argument(
            "--examples",
            type=int,
            default=5,
            help="Сколько ID показать для неоднозначных случаев (по умолчанию 5).",
        )

    def handle(self, *args, **options):
        plan = build_preferred_location_backfill()
        counts = Counter(row.source for row in plan)
        candidates = [row for row in plan if row.source in {"current", "history"}]
        inactive = [row for row in candidates if not row.location_is_usable]
        ambiguous = [row for row in plan if row.source == "ambiguous"]

        self.stdout.write("Preferred part locations")
        self.stdout.write(f"Already set: {counts['existing']}")
        self.stdout.write(f"From one current cell: {counts['current']}")
        self.stdout.write(f"From placement history: {counts['history']}")
        self.stdout.write(f"Ambiguous current cells: {counts['ambiguous']}")
        self.stdout.write(f"No placement evidence: {counts['none']}")
        self.stdout.write(f"Inactive or unavailable targets: {len(inactive)}")
        if ambiguous:
            examples = ", ".join(str(row.part_id) for row in ambiguous[: options["examples"]])
            self.stdout.write(f"Ambiguous part IDs: {examples}")

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("Dry-run only. Use --apply to create preferences.")
            )
            return

        with transaction.atomic():
            created = 0
            for row in candidates:
                _, was_created = PartPreferredLocation.objects.get_or_create(
                    part_type_id=row.part_id,
                    defaults={"location_id": row.location_id},
                )
                created += int(was_created)
        self.stdout.write(self.style.SUCCESS(f"Created preferred locations: {created}"))
