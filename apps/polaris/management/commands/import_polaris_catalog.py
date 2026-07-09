"""Import Polaris dealer price Excel into the reference catalog."""
from django.core.management.base import BaseCommand, CommandError

from apps.polaris.importer import PolarisImportError, import_catalog


class Command(BaseCommand):
    help = "Импортировать Polaris-прайс (Excel) в справочник. Остатки НЕ создаёт."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Путь к xlsx-файлу прайса")
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
                options["file"], commit=options["commit"], sheet=options["sheet"]
            )
        except PolarisImportError as exc:
            raise CommandError(str(exc)) from exc

        write = self.stdout.write
        mode = "ЗАПИСАНО (--commit)" if summary.mode == "commit" else "DRY-RUN (ничего не записано)"
        write(f"Режим: {mode}")
        write(f"Строк просмотрено: {summary.total_rows}")
        write(f"Строк данных: {summary.data_rows}")
        write(f"Создано: {summary.created}")
        write(f"Обновлено: {summary.updated}")
        write(f"Пропущено без изменений: {summary.skipped_unchanged}")
        write(f"Пропущено пустых: {summary.skipped_empty}")
        write(f"Без retail-цены: {summary.no_retail_price}")
        write(f"С superseded_number: {summary.with_superseded}")
        write(f"Ошибок: {summary.errors}")
        if summary.error_examples:
            write("Примеры ошибок:")
            for item in summary.error_examples:
                write(f"- {item}")
        self.stdout.write(self.style.SUCCESS("Импорт Polaris-каталога завершён."))

