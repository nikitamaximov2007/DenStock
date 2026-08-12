from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.operations.emergency_environment import validate_database_target
from apps.operations.failback import (
    FailbackError,
    complete_local_failback,
    configured_production_url,
    fetch_production_probe,
)

CONFIRM_PHRASE = "ПОДТВЕРДИТЬ-ЗАВЕРШЕННЫЙ-FAILBACK"


class Command(BaseCommand):
    help = "Подтвердить accepted production state и разрешить следующий standby refresh."

    def add_arguments(self, parser):
        parser.add_argument("--production-url")
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != CONFIRM_PHRASE:
            raise CommandError(f"Для подтверждения укажите --confirm {CONFIRM_PHRASE}")
        try:
            validate_database_target(mode="emergency-local")
            production_url = configured_production_url(options["production_url"])
            production = fetch_production_probe(
                production_url, token=settings.DENSTOCK_EMERGENCY_PROBE_TOKEN
            )
            session = complete_local_failback(production)
        except (FailbackError, RuntimeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Failback session {session.id} подтверждена. Standby refresh снова разрешён."
            )
        )
