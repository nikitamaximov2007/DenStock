"""Read-only audit of Customer phone-identity duplicates. Changes nothing."""

import csv

from django.core.management.base import BaseCommand

from apps.customers.dedup_audit import REVIEW_REQUIRED, audit_customer_phone_duplicates

GROUP_FIELDS = ("normalized_phone", "customer_id", "name", "sale_count", "repair_count",
                 "reservation_count", "request_count", "classification", "reason")


class Command(BaseCommand):
    help = (
        "Read-only audit: classify Customer cards by phone identity (unique / "
        "duplicate / missing / invalid) and group duplicates as safe-likely vs "
        "review-required. Never merges or writes anything."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="Путь для подробных строк дублей (CSV, UTF-8).")
        parser.add_argument(
            "--show", type=int, default=25,
            help="Сколько групп дублей напечатать подробно (0 - не печатать).",
        )

    def handle(self, *args, **options):
        report = audit_customer_phone_duplicates()
        write = self.stdout.write
        write("Аудит дублей телефона клиентов: только чтение, ничего не меняет и не объединяет")
        write(f"Всего карточек: {report.total_customers}")
        write(f"  с телефоном: {report.with_phone}")
        write(f"  валидный канонический телефон: {report.valid_normalized_phone}")
        write(f"  телефон не указан: {report.missing_phone}")
        write(f"  телефон некорректен (нет цифр): {report.invalid_phone}")
        write("")
        write(f"Групп дублей по телефону: {report.duplicate_groups}")
        write(f"  карточек внутри этих групп: {report.customers_in_duplicate_groups}")
        write(f"  групп с продажами: {report.duplicate_groups_with_sales}")
        write(f"  групп с ремонтами: {report.duplicate_groups_with_repairs}")
        write(f"  групп с продажами и ремонтами: {report.duplicate_groups_with_both}")
        write(
            f"  групп с заявками (CustomerRequest): "
            f"{report.duplicate_groups_referenced_by_request}"
        )
        write(
            f"  групп с конфликтующими именами (REVIEW-REQUIRED): "
            f"{report.groups_with_conflicting_names}"
        )
        write("")
        write(
            f"Возможные тёзки без телефона (подсказка, НЕ доказательство): "
            f"{len(report.name_only_groups)} групп"
        )

        if options["show"] and report.groups:
            write(f"\nПервые {min(options['show'], len(report.groups))} групп:")
            for group in report.groups[: options["show"]]:
                marker = "⚠ REVIEW" if group.classification == REVIEW_REQUIRED else "safe"
                write(f"  {group.normalized_phone} [{marker}] - {group.reason}")
                for row in group.customers:
                    write(
                        f"      #{row.customer_id} {row.name} - продаж {row.sale_count}, "
                        f"ремонтов {row.repair_count}, резервов {row.reservation_count}, "
                        f"заявок {row.request_count}"
                    )
        elif not report.groups:
            self.stdout.write(self.style.SUCCESS("\nДублей по телефону не найдено."))

        if options["csv"]:
            _write_csv(options["csv"], report)
            rows = sum(len(g.customers) for g in report.groups)
            write(f"\nПодробности: {options['csv']} ({rows} строк)")


def _write_csv(path, report):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(GROUP_FIELDS)
        for group in report.groups:
            for row in group.customers:
                writer.writerow([
                    group.normalized_phone, row.customer_id, row.name, row.sale_count,
                    row.repair_count, row.reservation_count, row.request_count,
                    group.classification, group.reason,
                ])
