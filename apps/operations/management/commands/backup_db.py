from django.core.management.base import BaseCommand, CommandError

from apps.operations import backup


class Command(BaseCommand):
    help = "Резервная копия БД (pg_dump -Fc для PostgreSQL, копия файла для SQLite)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir", default=None, help="Каталог назначения (по умолчанию backups/<timestamp>/)"
        )

    def handle(self, *args, **options):
        dest = options["dir"] or backup.new_run_dir()
        try:
            path = backup.backup_db(dest)
        except backup.OperationsError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"БД сохранена: {path}"))
