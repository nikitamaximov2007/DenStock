import csv

from django.core.management.base import BaseCommand

from apps.catalog.oil_candidate_audit import (
    OIL_ARTICLE_PREFIX,
    audit_oil_candidates,
    audit_oil_migration_readiness,
)

FIELDS = (
    "part_type_id",
    "name",
    "manufacturer",
    "category",
    "tracking_mode",
    "matched_numbers",
    "reason",
    "candidate_status",
)

STATUS_FIELDS = (
    "part_type_id",
    "name",
    "manufacturer",
    "tracking_mode",
    "oil_package_volume_l",
    "available_liters",
    "stock_lot_count",
    "movement_count",
    "sale_line_count",
    "repair_line_count",
    "configuration_status",
)


class Command(BaseCommand):
    help = (
        "Read-only audit: list PartTypes not marked is_oil whose article number "
        "starts with '337' (MOTUL oil packaging convention), plus the current "
        "configuration and real usage of every PartType already marked is_oil. "
        "A signal for a human to review, never an auto-classification. "
        "Changes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="Path for the candidate rows, UTF-8 CSV.")
        parser.add_argument(
            "--status-csv", help="Path for the already-oil status rows, UTF-8 CSV."
        )
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
        write(f"  можно безопасно пометить (нет остатков/истории): {report.safe_to_mark_count}")
        write(
            f"  требует решения владельца (остатки/история уже есть): "
            f"{report.needs_owner_review_count}"
        )
        write("Это подсказка по нумерации MOTUL, а не правило: решение - за человеком.")

        if options["show"] and report.rows:
            write(f"\nПервые {min(options['show'], len(report.rows))}:")
            for row in report.rows[: options["show"]]:
                write(
                    f"  #{row.part_type_id} {row.name} [{row.manufacturer or '-'}] "
                    f"({row.tracking_mode}) [{row.candidate_status}] - "
                    f"номера: {row.matched_numbers}"
                )
        elif not report.rows:
            self.stdout.write(self.style.SUCCESS("\nКандидатов нет."))

        if options["csv"]:
            _write_csv(options["csv"], FIELDS, report.rows)
            write(f"\nПодробности кандидатов: {options['csv']} ({len(report.rows)} строк)")

        status_report = audit_oil_migration_readiness()
        write(f"\nУже отмечено is_oil=True, статус конфигурации ({len(status_report.rows)}):")
        for row in status_report.rows:
            write(
                f"  #{row.part_type_id} {row.name} [{row.manufacturer or '-'}] "
                f"[{row.configuration_status}] - объём упаковки: "
                f"{row.oil_package_volume_l or 'не задан'}, в наличии: "
                f"{row.available_liters} л, лотов: {row.stock_lot_count}, "
                f"движений: {row.movement_count}, продаж: {row.sale_line_count}, "
                f"ремонтов: {row.repair_line_count}"
            )
        if options["status_csv"]:
            _write_csv(options["status_csv"], STATUS_FIELDS, status_report.rows)
            write(
                f"\nПодробности статуса: {options['status_csv']} "
                f"({len(status_report.rows)} строк)"
            )


def _write_csv(path, fields, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for row in rows:
            writer.writerow([getattr(row, name) for name in fields])
