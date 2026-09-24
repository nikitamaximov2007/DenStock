import csv

from django.core.management.base import BaseCommand

from apps.catalog.oil_candidate_audit import OIL_ARTICLE_PREFIX, audit_oil_candidates

FIELDS = (
    "part_type_id",
    "name",
    "manufacturer",
    "category",
    "tracking_mode",
    "matched_numbers",
    "reason",
)


class Command(BaseCommand):
    help = (
        "Read-only audit: list PartTypes not marked is_oil whose article number "
        "starts with '337' (MOTUL oil packaging convention). A signal for a "
        "human to review, never an auto-classification. Changes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="Path for the detailed UTF-8 CSV report.")
        parser.add_argument(
            "--show", type=int, default=50, help="How many candidates to print (0 hides rows)."
        )

    def handle(self, *args, **options):
        report = audit_oil_candidates()
        write = self.stdout.write
        write("Аудит кандидатов на «масло»: только чтение, ничего не меняет и не помечает")
        write(f"Уже отмечено is_oil=True: {report.already_oil_count}")
        write(
            f"Кандидатов (артикул начинается на {OIL_ARTICLE_PREFIX!r}, is_oil=False): "
            f"{report.candidate_count}"
        )
        write("Это подсказка по нумерации MOTUL, а не правило: решение - за человеком.")

        if options["show"] and report.rows:
            write(f"\nПервые {min(options['show'], len(report.rows))}:")
            for row in report.rows[: options["show"]]:
                write(
                    f"  #{row.part_type_id} {row.name} [{row.manufacturer or '-'}] "
                    f"({row.tracking_mode}) - номера: {row.matched_numbers}"
                )
        elif not report.rows:
            self.stdout.write(self.style.SUCCESS("\nКандидатов нет."))

        if options["csv"]:
            _write_csv(options["csv"], report.rows)
            write(f"\nПодробности: {options['csv']} ({len(report.rows)} строк)")


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        for row in rows:
            writer.writerow([getattr(row, name) for name in FIELDS])
