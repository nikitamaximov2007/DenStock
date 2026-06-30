from django.core.management.base import BaseCommand

from apps.operations import backup


class Command(BaseCommand):
    help = "Резервная копия media-файлов (mediafiles/ → media.tar.gz)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir", default=None, help="Каталог назначения (по умолчанию backups/<timestamp>/)"
        )

    def handle(self, *args, **options):
        dest = options["dir"] or backup.new_run_dir()
        path = backup.backup_media(dest)
        if path is None:
            self.stdout.write(self.style.WARNING("Media-файлов нет — нечего архивировать."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Media сохранены: {path}"))
