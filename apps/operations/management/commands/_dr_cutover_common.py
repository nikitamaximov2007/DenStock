import json

from django.core.management.base import BaseCommand, CommandError

from apps.operations.offline_restore import StageRestoreError


class DRCommand(BaseCommand):
    """Offline DR command: no system checks (they would open guarded connections)."""

    requires_system_checks = []

    def add_journal_argument(self, parser):
        parser.add_argument("stage_journal", help="Журнал dr_stage_restore")

    def add_writers_argument(self, parser):
        parser.add_argument(
            "--writers-stopped",
            action="store_true",
            help="Оператор подтверждает: web, catalog-web, оба бота и proxy остановлены",
        )

    def run_service(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except StageRestoreError as exc:
            raise CommandError(str(exc)) from exc

    def write_json(self, data):
        self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2, default=str))
