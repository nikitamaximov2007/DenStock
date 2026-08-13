import json

from django.core.management.base import BaseCommand

from apps.operations.emergency_state import business_state_marker, migration_state


class Command(BaseCommand):
    help = "Read-only JSON marker for standby and failback validation."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", required=True)

    def handle(self, *args, **options):
        migrations = migration_state()
        payload = {
            "migration_fingerprint": migrations["fingerprint"],
            "data_state": business_state_marker(),
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
