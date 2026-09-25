"""Read-only audit of PartAnalog relations and their evidence."""

import csv

from django.core.management.base import BaseCommand

from apps.catalog.analog_audit import audit_part_analogs

FIELDS = ("original_id", "analog_id", "relation_types")


class Command(BaseCommand):
    help = (
        "Read-only audit of the part-analog relation/evidence system: counts "
        "by verification state, relation type and evidence source, plus "
        "integrity checks (duplicates, inactive targets, conflicting "
        "supersessions). Never merges, confirms, rejects or writes anything."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", help="Путь для строк-конфликтов (CSV, UTF-8).")
        parser.add_argument(
            "--show", type=int, default=25,
            help="Сколько примеров конфликтов напечатать (0 - не печатать).",
        )

    def handle(self, *args, **options):
        report = audit_part_analogs()
        write = self.stdout.write
        write("Аудит связей аналогов: только чтение, ничего не меняет")
        write(f"Всего связей: {report.total_relations}")
        write("")
        write("По статусу проверки:")
        for state, count in sorted(report.by_verification.items()):
            write(f"  {state}: {count}")
        write("")
        write("По типу связи:")
        for relation_type, count in sorted(report.by_relation_type.items()):
            write(f"  {relation_type}: {count}")
        write("")
        write("По типу источника (провенанс):")
        if report.by_source_type:
            for source_type, count in sorted(report.by_source_type.items()):
                write(f"  {source_type}: {count}")
        else:
            write("  провенанс не записан ни для одной связи")
        write("")
        write(f"Связей без провенанса вовсе: {report.relations_without_evidence}")
        write(
            "Связей с неактивной деталью на одной из сторон: "
            f"{report.relations_with_inactive_target}"
        )
        write(f"Дублирующихся логических связей (ожидается 0): {report.duplicate_relations}")
        write(
            "Пар с несколькими типами связи (не конфликт, справочно): "
            f"{len(report.multi_type_pairs)}"
        )
        write(
            f"Противоречивых замен (§21, требуют разбора): "
            f"{len(report.conflicting_supersessions)}"
        )
        write(f"Связей, доступных публичному каталогу: {report.public_eligible_relations}")
        write(
            f"Провенанс с ценой источника без валюты: {report.evidence_price_missing_currency}"
        )
        write(
            "Провенанс с ценой источника без даты наблюдения: "
            f"{report.evidence_price_missing_observed_at}"
        )

        if options["show"] and report.conflicting_supersessions:
            shown = min(options["show"], len(report.conflicting_supersessions))
            write(f"\nПротиворечивые замены, первые {shown}:")
            for row in report.conflicting_supersessions[: options["show"]]:
                write(f"  #{row.original_id} {row.original_name} -> {', '.join(row.targets)}")
        elif options["show"]:
            self.stdout.write(self.style.SUCCESS("\nПротиворечивых замен не найдено."))

        if options["csv"]:
            _write_csv(options["csv"], report)
            rows = len(report.conflicting_supersessions)
            write(f"\nПодробности: {options['csv']} ({rows} строк)")


def _write_csv(path, report):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("original_id", "original_name", "targets"))
        for row in report.conflicting_supersessions:
            writer.writerow([row.original_id, row.original_name, "; ".join(row.targets)])
        writer.writerow([])
        writer.writerow(FIELDS)
        for pair in report.multi_type_pairs:
            writer.writerow([pair.original_id, pair.analog_id, "; ".join(pair.relation_types)])
