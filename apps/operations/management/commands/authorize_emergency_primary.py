from django.core.management.base import BaseCommand, CommandError

from apps.operations.emergency_primary import (
    EmergencyPrimaryAuthorizationError,
    authorize_emergency_primary,
)


class Command(BaseCommand):
    help = "Явно назначить единственный Emergency Primary в production."

    def add_arguments(self, parser):
        parser.add_argument("--workstation-id", required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != "НАЗНАЧИТЬ-EMERGENCY-PRIMARY":
            raise CommandError("Нужна точная фраза НАЗНАЧИТЬ-EMERGENCY-PRIMARY.")
        try:
            state = authorize_emergency_primary(options["workstation_id"], actor=options["actor"])
        except EmergencyPrimaryAuthorizationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Emergency Primary назначен: {state.authorized_emergency_primary_id}; "
            f"epoch={state.primary_authorization_epoch}"
        )
