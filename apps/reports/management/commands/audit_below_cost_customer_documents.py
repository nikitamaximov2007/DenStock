import csv

from django.core.management.base import BaseCommand

from apps.reports.below_cost_audit import audit_below_cost_customer_documents


class Command(BaseCommand):
    help = "Read-only audit of completed sale and repair part lines below accounting cost."

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="Path for the detailed UTF-8 CSV report.")
        parser.add_argument(
            "--show", type=int, default=25, help="How many rows to print (0 hides rows)."
        )

    def handle(self, *args, **options):
        rows = audit_below_cost_customer_documents()
        sales = [row for row in rows if row.document_kind == "sale"]
        repairs = [row for row in rows if row.document_kind == "repair"]
        self.stdout.write("Below-cost customer document audit: read-only")
        self.stdout.write(f"Sale lines: {len(sales)}")
        self.stdout.write(f"Repair issue lines: {len(repairs)}")
        for row in rows[: options["show"]]:
            self.stdout.write(
                f"{row.document_kind} {row.document_number} line {row.line_id}: "
                f"{row.customer_line_amount_rub} < {row.accounting_line_cost_rub}; "
                f"{row.probable_cause}"
            )
        if options["csv"]:
            _write_csv(options["csv"], rows)
            self.stdout.write(f"Details: {options['csv']} ({len(rows)} rows)")


def _write_csv(path, rows):
    fields = tuple(rows[0].asdict()) if rows else (
        "document_kind",
        "document_id",
        "document_number",
        "document_date",
        "customer",
        "line_id",
        "article",
        "part_name",
        "quantity",
        "source_kind",
        "source_id",
        "customer_unit_price_rub",
        "customer_line_amount_rub",
        "accounting_unit_cost_rub",
        "accounting_line_cost_rub",
        "delta_rub",
        "margin_percent",
        "inventory_history",
        "manual_override_identifiable",
        "receipt_customer_price_snapshot_rub",
        "current_canonical_price_rub",
        "probable_cause",
        "explanation",
    )
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(row.asdict() for row in rows)
