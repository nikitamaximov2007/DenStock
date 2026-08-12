from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.operations.emergency_state import record_event
from apps.operations.models import DeploymentState
from apps.operations.write_guard import acquire_failover_lock, lifecycle_write

ENABLE_PHRASE = "PRODUCTION-ТОЛЬКО-ЧТЕНИЕ"
DISABLE_PHRASE = "ОПАСНО-РАЗРЕШИТЬ-ЗАПИСЬ"


class Command(BaseCommand):
    help = "Controlled production maintenance write lock."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--enable", action="store_true")
        group.add_argument("--disable", action="store_true")
        parser.add_argument("--confirm", required=True)
        parser.add_argument("--reason", default="controlled failover")

    def handle(self, *args, **options):
        if settings.DENSTOCK_MODE != "production":
            raise CommandError("Команда разрешена только в production mode.")
        expected = ENABLE_PHRASE if options["enable"] else DISABLE_PHRASE
        if options["confirm"] != expected:
            raise CommandError(f"Для подтверждения укажите --confirm {expected}")
        with transaction.atomic(), lifecycle_write():
            acquire_failover_lock(exclusive=True)
            state = DeploymentState.objects.select_for_update().get(
                pk=DeploymentState.SINGLETON_PK
            )
            target = (
                DeploymentState.WriteState.MAINTENANCE
                if options["enable"]
                else DeploymentState.WriteState.NORMAL
            )
            expected_current = (
                DeploymentState.WriteState.NORMAL
                if options["enable"]
                else DeploymentState.WriteState.MAINTENANCE
            )
            if state.write_state != expected_current:
                raise CommandError(
                    f"Переход {state.write_state} -> {target} запрещён."
                )
            state.write_state = target
            state.state_reason = options["reason"][:255] if options["enable"] else ""
            state.state_changed_at = timezone.now()
            state.save(
                update_fields=["write_state", "state_reason", "state_changed_at", "updated_at"]
            )
            record_event(
                "production_maintenance",
                "enabled" if options["enable"] else "disabled",
                details={"reason": state.state_reason},
            )
        self.stdout.write(self.style.SUCCESS(f"Production write state: {target}"))
