from django.core.management.base import BaseCommand, CommandError

from apps.operations.standby import StandbyError, refresh_standby


class Command(BaseCommand):
    help = "Обновить local emergency standby из verified production backup."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="Локальный каталог или rclone remote")
        parser.add_argument("--run-id", default=None, help="Конкретный backup run")

    def handle(self, *args, **options):
        try:
            active = refresh_standby(options["source"], run_id=options["run_id"])
        except StandbyError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Standby обновлён: backup {active['backup_run_id']}, "
                f"создан {active['backup_created_at']}."
            )
        )
