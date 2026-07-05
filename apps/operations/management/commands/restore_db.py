from django.core.management.base import BaseCommand, CommandError

from apps.operations import backup


class Command(BaseCommand):
    help = "Восстановить БД из дампа. ОПАСНО: перезаписывает данные. Требует --yes."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Путь к файлу дампа (db.dump / db.sqlite3)")
        parser.add_argument("--yes", action="store_true", help="Подтвердить перезапись данных")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError(
                "ВНИМАНИЕ: восстановление ПЕРЕЗАПИШЕТ текущую БД. "
                "Повторите команду с флагом --yes для подтверждения."
            )
        try:
            warnings = backup.restore_db(options["source"])
        except backup.OperationsError as exc:
            raise CommandError(str(exc)) from exc
        for warning in warnings:
            self.stdout.write(self.style.WARNING(f"Предупреждение: {warning}"))
        self.stdout.write(self.style.SUCCESS("БД восстановлена из бэкапа."))
