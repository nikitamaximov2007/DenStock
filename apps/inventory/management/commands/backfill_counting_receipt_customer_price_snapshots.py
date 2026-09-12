"""Controlled recovery of proven historical receipt customer prices."""

import csv
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.inventory.historical_price_backfill import (
    apply_historical_price_backfill,
    build_historical_price_backfill_plan,
)


class Command(BaseCommand):
    help = (
        "Восстановить только доказуемые снимки цен из проведённых пересчётов "
        "(dry-run по умолчанию)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Записать только однозначно доказанные снимки."
        )
        parser.add_argument("--csv", help="Путь для полного аудиторского CSV-плана.")

    def handle(self, *args, **options):
        try:
            plan = (
                apply_historical_price_backfill()
                if options["apply"]
                else build_historical_price_backfill_plan()
            )
        except RuntimeError as error:
            raise CommandError(str(error)) from error
        counts, quantities = plan.counts, plan.quantities
        eligible_parts = len({row.part_type_id for row in plan.eligible})
        self.stdout.write("Historical counting receipt-price backfill")
        self.stdout.write(
            f"In-scope available StockLots: {len(plan.rows)} / units: {quantities.total()}"
        )
        self.stdout.write(
            f"Eligible unique evidence: {counts['eligible']} / units: "
            f"{quantities['eligible']} / PartTypes: {eligible_parts}"
        )
        existing = sum(1 for row in plan.rows if row.reason == "snapshot_exists")
        self.stdout.write(f"Already snapshotted: {existing}")
        skipped_reasons = Counter(
            row.reason
            for row in plan.rows
            if row.outcome == "skipped" and row.reason != "snapshot_exists"
        )
        for reason, count in sorted(skipped_reasons.items()):
            self.stdout.write(f"Skipped {reason}: {count}")
        if options["csv"]:
            destination = Path(options["csv"])
            if not destination.parent.exists():
                raise CommandError(f"CSV directory does not exist: {destination.parent}")
            with destination.open("w", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow(
                    [
                        "lot_id", "part_type_id", "quantity", "receipt_id", "session_id",
                        "counting_line_id", "evidence_price", "outcome", "reason",
                        "previous_snapshot",
                    ]
                )
                for row in plan.rows:
                    writer.writerow(
                        [
                            row.lot_id, row.part_type_id, row.quantity, row.receipt_id,
                            row.session_id, row.counting_line_id, row.evidence_price,
                            row.outcome, row.reason, row.previous_snapshot,
                        ]
                    )
            self.stdout.write(f"Audit CSV: {destination}")
        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Applied snapshots: {counts['eligible']}"))
        else:
            self.stdout.write(
                self.style.WARNING("Dry-run only. Use --apply after reviewing this exact plan.")
            )
