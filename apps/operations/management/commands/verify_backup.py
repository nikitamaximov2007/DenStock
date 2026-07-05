"""Проверка целостности бэкапа перед восстановлением (read-only).

    python manage.py verify_backup <run_id>

Ничего не меняет: печатает отчёт проверки и завершается ошибкой, если бэкап
непригоден для восстановления.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.operations.restore import verify_backup

MARKS = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}


class Command(BaseCommand):
    help = "Проверить бэкап (manifest, файлы, движок) без каких-либо изменений."

    def add_arguments(self, parser):
        parser.add_argument("run_id", help="Имя каталога бэкапа внутри BACKUP_ROOT")

    def handle(self, *args, **options):
        report = verify_backup(options["run_id"])
        for label, state in report.checks:
            self.stdout.write(f"{MARKS[state]} {label}")
        if not report.ok:
            raise CommandError(
                "Бэкап НЕ пригоден для восстановления: " + "; ".join(report.errors)
            )
        self.stdout.write(self.style.SUCCESS(f"Бэкап {report.run_id} пригоден для восстановления."))
