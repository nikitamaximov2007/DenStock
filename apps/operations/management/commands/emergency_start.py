from django.core.management.base import BaseCommand, CommandError

from apps.operations.emergency_lifecycle import EmergencyLifecycleError, start_offline_session
from apps.operations.models import OfflineSession

CONFIRM_PHRASE = "НАЧАТЬ-АВТОНОМНУЮ-РАБОТУ"


class Command(BaseCommand):
    help = "Начать local offline session из active verified standby."

    def add_arguments(self, parser):
        parser.add_argument(
            "--kind",
            choices=[OfflineSession.Kind.PLANNED, OfflineSession.Kind.UNPLANNED],
            required=True,
        )
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != CONFIRM_PHRASE:
            raise CommandError(f"Для подтверждения укажите --confirm {CONFIRM_PHRASE}")
        try:
            session = start_offline_session(kind=options["kind"], actor="operator")
        except EmergencyLifecycleError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.WARNING(
                f"АВТОНОМНЫЙ РЕЖИМ активен. Session {session.id}. "
                "Все складские операции должны выполняться только локально."
            )
        )
