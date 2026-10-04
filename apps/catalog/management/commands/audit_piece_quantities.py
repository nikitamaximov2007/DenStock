"""Найти дробные количества у штучных деталей. Только чтение."""

from django.core.management.base import BaseCommand

from apps.catalog.piece_quantity_audit import audit_piece_quantities
from apps.catalog.quantity_units import format_quantity


class Command(BaseCommand):
    help = (
        "Показать строки продаж, заявок, ремонтов, броней, списаний и остатки лотов, "
        "где у штучной (не масляной) детали дробное количество. Ничего не изменяет."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--show",
            type=int,
            default=50,
            help="Сколько строк напечатать подробно (0 - только сводка).",
        )

    def handle(self, *args, **options):
        report = audit_piece_quantities()
        write = self.stdout.write
        write("Аудит штучных количеств: только чтение, ни одна строка не изменена")
        write("Правило: у детали без признака «масло» количество - целое число штук.")
        write("")
        write("Штучные детали по единицам измерения:")
        for unit, parts in report.piece_parts_by_unit.items():
            write(f"  {unit}: {parts}")
        write("Масло по единицам (число в заявке с каталога - упаковки, в остатке - литры):")
        for unit, parts in report.oil_parts_by_unit.items():
            write(f"  {unit}: {parts}")
        write("")
        write(f"Дробных штучных строк всего: {report.total}")
        for source, statuses in report.by_source.items():
            count = sum(statuses.values())
            detail = ", ".join(f"{status}: {n}" for status, n in sorted(statuses.items()))
            write(f"  {source}: {count}" + (f" ({detail})" if detail else ""))
        show = max(options["show"], 0)
        if show and report.rows:
            write("")
            write("Строки (таблица, id строки, id документа, статус, id детали, единица, кол-во):")
            for row in report.rows[:show]:
                quantity = format_quantity(row.quantity, None)
                write(
                    f"  {row.source}; {row.row_id}; {row.document_id or '-'}; {row.status}; "
                    f"{row.part_type_id}; {row.unit or '-'}; {quantity}"
                )
            if report.total > show:
                write(f"  ... и ещё {report.total - show}")
        write("")
        verdict = "есть строки для разбора." if report.total else "дробных штучных строк нет."
        write(f"Итог: {verdict}")
