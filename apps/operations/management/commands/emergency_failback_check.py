import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.operations import backup
from apps.operations.emergency_environment import validate_database_target
from apps.operations.failback import FailbackError, evaluate_failback, fetch_production_probe
from apps.operations.models import OfflineSession


class Command(BaseCommand):
    help = "Read-only failback eligibility check. Production restore не выполняется."

    def add_arguments(self, parser):
        parser.add_argument("--production-url")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            validate_database_target(mode="emergency-local")
            session = OfflineSession.objects.filter(
                status__in=[
                    OfflineSession.Status.FROZEN,
                    OfflineSession.Status.ELIGIBLE,
                    OfflineSession.Status.CONFLICT,
                    OfflineSession.Status.BLOCKED,
                ]
            ).first()
            if not session or not session.final_backup_run_id:
                raise FailbackError("Нет frozen offline session с final backup.")
            production_url = options["production_url"] or settings.DENSTOCK_PRODUCTION_URL
            if not production_url:
                raise FailbackError("DENSTOCK_PRODUCTION_URL не задан.")
            configured_url = settings.DENSTOCK_PRODUCTION_URL.rstrip("/")
            if configured_url and production_url.rstrip("/") != configured_url:
                raise FailbackError(
                    "Production URL отличается от DENSTOCK_PRODUCTION_URL; token не отправлен."
                )
            production = fetch_production_probe(
                production_url, token=settings.DENSTOCK_EMERGENCY_PROBE_TOKEN
            )
            root = backup.backup_root().resolve()
            final_run = (root / session.final_backup_run_id).resolve()
            if final_run.parent != root or not final_run.is_dir():
                raise FailbackError("Final backup path находится вне BACKUP_ROOT.")
            decision = evaluate_failback(session, production, final_run)
        except (FailbackError, RuntimeError) as exc:
            raise CommandError(str(exc)) from exc
        payload = decision.as_dict()
        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            self.stdout.write(f"Failback status: {decision.status.upper()}")
            for reason in decision.reasons:
                self.stdout.write(f"- {reason}")
        if not decision.eligible:
            raise CommandError(
                "Automatic production overwrite запрещён. Требуется reconciliation."
            )
