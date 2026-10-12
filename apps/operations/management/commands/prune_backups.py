from django.core.management.base import BaseCommand, CommandError

from apps.operations import backup


class Command(BaseCommand):
    help = "Удалить старые локальные backup runs после подтверждённой offsite-проверки."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--keep-last", type=int, required=True)

    def handle(self, *args, **options):
        keep_last = options["keep_last"]
        if keep_last <= 0:
            raise CommandError("--keep-last должен быть больше нуля.")
        removed = backup.prune_old_runs(backup.backup_root(), keep_last)
        self.stdout.write(self.style.SUCCESS(f"Удалено старых backup runs: {len(removed)}"))
