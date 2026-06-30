from django.core.management.base import BaseCommand, CommandError

from apps.operations import backup


class Command(BaseCommand):
    help = "Восстановить media из архива. ОПАСНО: перезаписывает mediafiles/. Требует --yes."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Путь к архиву media.tar.gz")
        parser.add_argument("--yes", action="store_true", help="Подтвердить перезапись media")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError(
                "ВНИМАНИЕ: восстановление ПЕРЕЗАПИШЕТ текущие media-файлы. "
                "Повторите команду с флагом --yes для подтверждения."
            )
        try:
            backup.restore_media(options["source"])
        except backup.OperationsError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Media восстановлены из бэкапа."))
