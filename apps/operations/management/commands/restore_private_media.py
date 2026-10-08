"""Explicit restore for the private volume; never runs during a normal backup."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.operations import backup


class Command(BaseCommand):
    help = "Восстановить private_media из архива. Требует --yes."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Путь к архиву private_media.tar.gz")
        parser.add_argument("--yes", action="store_true")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Восстановление private_media требует --yes.")
        try:
            backup.restore_media(options["source"], media_root=settings.PRIVATE_MEDIA_ROOT)
        except backup.OperationsError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Private media восстановлены."))
