from django.core.management.base import BaseCommand, CommandError

from apps.operations.emergency_primary import (
    EmergencyPrimaryAuthorizationError,
    revoke_emergency_primary,
)


class Command(BaseCommand):
    help = "Явно отозвать Emergency Primary в production."

    def add_arguments(self, parser):
        parser.add_argument("--actor", required=True)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != "ОТОЗВАТЬ-EMERGENCY-PRIMARY":
            raise CommandError("Нужна точная фраза ОТОЗВАТЬ-EMERGENCY-PRIMARY.")
        try:
            state = revoke_emergency_primary(actor=options["actor"])
        except EmergencyPrimaryAuthorizationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Emergency Primary отозван; epoch={state.primary_authorization_epoch}")
