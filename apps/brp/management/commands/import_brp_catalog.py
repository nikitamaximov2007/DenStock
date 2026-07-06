"""Импорт дилерского прайса BRP в справочник (НЕ в складские остатки).

    python manage.py import_brp_catalog path/to/file.xlsx --dry-run
    python manage.py import_brp_catalog path/to/file.xlsx --commit

Без --commit выполняется dry-run: файл разбирается полностью, печатается
итог, но в базу НИЧЕГО не пишется. Перед боевым импортом на сервере сначала
создайте бэкап (см. docs/operations/brp-catalog-import.md).
"""
from django.core.management.base import BaseCommand, CommandError

from apps.brp.importer import BrpImportError, import_catalog


class Command(BaseCommand):
    help = "Импортировать BRP-прайс (Excel) в справочник. Остатки НЕ создаёт."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Путь к xlsx-файлу прайса (в Git не коммитится)")
        parser.add_argument(
            "--commit", action="store_true",
            help="Записать изменения (без флага: dry-run, ничего не пишется)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Явно указать dry-run (поведение по умолчанию)",
        )
        parser.add_argument("--sheet", default=None, help="Имя листа (по умолчанию первый)")

    def handle(self, *args, **options):
        if options["commit"] and options["dry_run"]:
            raise CommandError("Выберите одно: --dry-run ИЛИ --commit.")
        try:
            summary = import_catalog(
                options["path"], commit=options["commit"], sheet=options["sheet"]
            )
        except BrpImportError as exc:
            raise CommandError(str(exc)) from exc

        write = self.stdout.write
        mode = "ЗАПИСАНО (--commit)" if summary.mode == "commit" else "DRY-RUN (ничего не записано)"
        write(f"Режим: {mode}")
        write(f"Строк просмотрено: {summary.total_rows_scanned}")
        write(f"Строк данных: {summary.data_rows}")
        write(f"Создано: {summary.created}")
        write(f"Обновлено: {summary.updated}")
        write(f"Пропущено без изменений: {summary.skipped_unchanged}")
        write(f"Пропущено пустых: {summary.skipped_empty}")
        write(f"Дубликатов Material_No: {summary.duplicates}")
        write(f"Уникальных номеров: {summary.unique_materials}")
        write(f"С розничной ценой: {summary.with_retail_price}")
        write(f"С оптовой ценой: {summary.with_wholesale_price}")
        write(f"С заменой номера: {summary.with_replacement}")
        statuses = ", ".join(
            f"{name}={count}" for name, count in sorted(summary.status_counts.items())
        )
        write(f"Статусы: {statuses or 'нет'}")
        self.stdout.write(self.style.SUCCESS("Импорт BRP-каталога завершён."))
