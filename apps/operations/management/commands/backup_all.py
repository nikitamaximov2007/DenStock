from django.core.management.base import BaseCommand, CommandError

from apps.operations import backup


class Command(BaseCommand):
    help = "Полный бэкап: БД + media + manifest.json в одном каталоге backups/<timestamp>/."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-last", type=int, default=None,
            help="Оставить только N последних бэкапов (удалить старые).",
        )

    def handle(self, *args, **options):
        try:
            run = backup.backup_all(keep_last=options["keep_last"])
        except backup.OperationsError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Полный бэкап готов: {run}"))
