import csv

from django.core.management.base import BaseCommand

from apps.reports.cost_provenance_audit import audit_sale_cost_provenance

FIELDS = (
    "sale_id",
    "sale_number",
    "sold_at",
    "line_id",
    "part_type_id",
    "part_name",
    "quantity",
    "unit_price_rub",
    "provenance",
    "unmarked_price_source",
    "unmarked_unit_price_rub_snapshot",
    "unmarked_usd_rate_snapshot",
)


class Command(BaseCommand):
    help = (
        "Read-only audit of the cost basis (\"Себестоимость\") behind every "
        "completed SaleLine: captured live, reconstructed via the owner-"
        "approved legacy 105 ₽/USD backfill, or genuinely unknown. Changes "
        "nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="Path for the detailed UTF-8 CSV report.")
        parser.add_argument(
            "--show", type=int, default=25,
            help="How many unknown-cost lines to print in detail (0 hides rows).",
        )

    def handle(self, *args, **options):
        report = audit_sale_cost_provenance()
        write = self.stdout.write
        write("Аудит происхождения себестоимости продаж: только чтение, ничего не меняет")
        write(f"Всего проведённых строк: {report.total_lines}")
        write(
            f"  живая база (на момент продажи): {report.live_count} "
            f"(известная выручка: {report.live_known_revenue} ₽)"
        )
        write(
            f"  legacy-реконструкция 105 ₽/$ (owner-approved, действительна): "
            f"{report.legacy_105_count} (известная выручка: {report.legacy_105_known_revenue} ₽)"
        )
        write(
            f"  база неизвестна (нет каталожной связи): {report.unknown_count} "
            f"(выручка вне известного scope: {report.unknown_revenue} ₽)"
        )
        write("")
        write("По источнику (brp/polaris/aftermarket/arctic_cat/-):")
        for source, count in sorted(report.by_source.items()):
            write(f"  {source}: {count}")

        if options["show"]:
            unknown_rows = [row for row in report.rows if row.provenance == "unknown"]
            if unknown_rows:
                shown = min(options["show"], len(unknown_rows))
                write(f"\nСтроки без известной базы, первые {shown}:")
                for row in unknown_rows[: options["show"]]:
                    write(
                        f"  продажа {row.sale_number} строка #{row.line_id}: "
                        f"{row.part_name} × {row.quantity} по {row.unit_price_rub} ₽"
                    )
            else:
                self.stdout.write(self.style.SUCCESS("\nСтрок без известной базы нет."))

        if options["csv"]:
            _write_csv(options["csv"], report.rows)
            write(f"\nПодробности: {options['csv']} ({len(report.rows)} строк)")


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        for row in rows:
            writer.writerow([getattr(row, name) for name in FIELDS])
