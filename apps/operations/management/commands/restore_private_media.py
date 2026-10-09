from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.operations import restore
from apps.operations.private_media import PrivateMediaError, restore_archive


class Command(BaseCommand):
    help = (
        "Восстановить private_media из бэкапа (замена содержимого целиком, с откатом "
        "при ошибке). ОПАСНО: заменяет текущие приватные файлы. Требует --yes."
    )

    def add_arguments(self, parser):
        parser.add_argument("run_id", help="Имя каталога бэкапа внутри BACKUP_ROOT")
        parser.add_argument("--yes", action="store_true", help="Подтвердить замену файлов")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError(
                "ВНИМАНИЕ: восстановление ЗАМЕНИТ текущие private_media. "
                "Повторите команду с флагом --yes для подтверждения."
            )
        report = restore.verify_backup(options["run_id"])
        if not report.ok:
            raise CommandError("Бэкап не прошёл проверку: " + "; ".join(report.errors))
        if not report.private_media_file:
            raise CommandError("В этой копии нет private_media: восстанавливать нечего.")
        try:
            inventory = restore_archive(
                restore._safe_run_dir(options["run_id"]) / report.private_media_file,
                settings.PRIVATE_MEDIA_ROOT,
                expected=restore._manifest_private_inventory(report.manifest),
            )
        except PrivateMediaError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Private media восстановлены: файлов {inventory.files}, байт {inventory.bytes}."
        ))
