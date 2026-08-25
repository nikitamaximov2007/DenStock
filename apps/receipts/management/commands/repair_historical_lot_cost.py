"""Apply a deliberately narrow, receipt-proven historical lot-cost correction."""

from django.core.management.base import BaseCommand, CommandError

from apps.receipts.remediation import (
    HistoricalLotCostRemediationError,
    apply_historical_lot_cost_remediation,
    plan_historical_lot_cost_remediation,
)


class Command(BaseCommand):
    help = "Исправить proven себестоимость одного лота и только его производные снимки."

    def add_arguments(self, parser):
        parser.add_argument("--lot-id", type=int, required=True)
        parser.add_argument("--receipt-line-id", type=int, required=True)
        parser.add_argument("--expected-old-cost", required=True)
        parser.add_argument("--new-cost", required=True)
        parser.add_argument("--source-reference", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            plan = plan_historical_lot_cost_remediation(
                lot_id=options["lot_id"],
                receipt_line_id=options["receipt_line_id"],
                expected_old_cost=options["expected_old_cost"],
                new_cost=options["new_cost"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Источник: {options['source_reference']}")
        self.stdout.write(
            f"Лот {plan.lot_id}: {plan.old_cost} -> {plan.new_cost}; "
            f"лоты={list(plan.lot_ids)}, ремонты={list(plan.repair_line_ids)}, "
            f"продажи={list(plan.sale_line_ids)}, возвраты={list(plan.return_line_ids)}"
        )
        if not options["apply"]:
            self.stdout.write("DRY-RUN: база данных не изменена.")
            return
        try:
            apply_historical_lot_cost_remediation(plan)
        except HistoricalLotCostRemediationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Исправление применено."))
