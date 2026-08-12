import json
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.operations.models import DeploymentState, OfflineSession
from apps.operations.standby import StandbyError, load_control


class Command(BaseCommand):
    help = "Показать local emergency standby и offline session status."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            control = load_control()
        except StandbyError as exc:
            raise CommandError(str(exc)) from exc
        active = control.get("active_standby")
        session = OfflineSession.objects.first()
        deployment = DeploymentState.get_solo()
        payload = {
            "mode": settings.DENSTOCK_MODE,
            "instance_id": settings.DENSTOCK_INSTANCE_ID,
            "write_state": deployment.write_state,
            "standby": active,
            "session": (
                {
                    "id": str(session.id),
                    "status": session.status,
                    "started_at": session.started_at.isoformat(),
                }
                if session
                else None
            ),
        }
        if active:
            created = datetime.fromisoformat(active["backup_created_at"])
            if timezone.is_naive(created):
                created = timezone.make_aware(created)
            payload["standby_age_hours"] = round(
                (timezone.now() - created).total_seconds() / 3600, 1
            )
            payload["standby_stale"] = (
                payload["standby_age_hours"]
                > settings.DENSTOCK_EMERGENCY_STALE_WARNING_HOURS
            )
        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return
        self.stdout.write(f"Режим: {payload['mode']}")
        self.stdout.write(f"Instance: {payload['instance_id']}")
        self.stdout.write(f"Запись: {payload['write_state']}")
        if active:
            self.stdout.write(f"Backup run: {active['backup_run_id']}")
            self.stdout.write(f"Backup time: {active['backup_created_at']}")
            self.stdout.write(f"Возраст копии: {payload['standby_age_hours']} ч")
            self.stdout.write(f"Production commit: {active['app_commit']}")
            if payload["standby_stale"]:
                self.stdout.write(self.style.WARNING("ВНИМАНИЕ: аварийная копия устарела."))
        else:
            self.stdout.write(self.style.WARNING("Проверенной standby-копии нет."))
