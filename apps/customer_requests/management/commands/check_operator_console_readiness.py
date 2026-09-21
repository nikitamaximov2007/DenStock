"""Read-only pre-activation check for the mobile operator console."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Count
from django.utils import timezone

from apps.customer_requests.models import StaffMessengerBinding, StaffMessengerPairingToken
from apps.operations.models import MaxBotRuntime, TelegramBotRuntime

REQUIRED_MIGRATIONS = {
    "0017_operatorconsoleruntime_and_more",
    "0018_staffmessengerbinding_operator_mode",
    "0019_customerrequest_current_responder_control_source_and_more",
    "0020_operator_context_safety",
    "0021_two_provider_pairing_slots",
}
HEARTBEAT_MAX_AGE = timedelta(minutes=3)


def _redacted_identity(value: int) -> str:
    """A stable diagnostic hint which never prints a full provider identity."""
    tail = str(value)[-4:]
    return f"…{tail}"


class Command(BaseCommand):
    help = "Read-only readiness check for the mobile operator console; no secrets are shown."

    def handle(self, *args, **options):
        now = timezone.now()
        blockers: list[str] = []
        warnings: list[str] = []

        self.stdout.write(
            "CUSTOMER_OPERATOR_CONSOLE_ENABLED="
            f"{str(bool(settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED)).lower()}"
        )

        try:
            applied = set(
                MigrationRecorder(connections["default"]).migration_qs.filter(
                    app="customer_requests", name__in=REQUIRED_MIGRATIONS
                ).values_list("name", flat=True)
            )
            missing = sorted(REQUIRED_MIGRATIONS - applied)
            if missing:
                blockers.append("не применены миграции: " + ", ".join(missing))
            else:
                self.stdout.write("миграции 0017-0021: применены")

            active = StaffMessengerBinding.objects.select_related("user").filter(is_active=True)
            self.stdout.write(f"активных привязок: {active.count()}")
            by_label = {}
            for binding in active:
                by_label.setdefault(binding.customer_visible_label, {})[binding.provider] = True
            for label in ("Денис", "Рим"):
                status = by_label.get(label, {})
                self.stdout.write(
                    f"{label}: Telegram - "
                    f"{'подключён' if status.get('telegram') else 'не подключён'}; "
                    f"MAX - {'подключён' if status.get('max') else 'не подключён'}"
                )
            for binding in active.order_by("customer_visible_label", "provider", "pk"):
                state = "Telegram" if binding.provider == "telegram" else "MAX"
                self.stdout.write(
                    f"  {binding.customer_visible_label}: {state} "
                    f"({_redacted_identity(binding.provider_user_id)})"
                )
                if not binding.user.is_active or not binding.user.can_manage_sales:
                    blockers.append(
                        f"активная привязка «{binding.customer_visible_label}» принадлежит "
                        "неподходящему сотруднику"
                    )

            duplicates = list(
                StaffMessengerBinding.objects.values("provider", "provider_user_id")
                .annotate(total=Count("pk"))
                .filter(total__gt=1)
            )
            if duplicates:
                blockers.append("обнаружены дубли provider identity")
            duplicate_users = list(
                StaffMessengerBinding.objects.values("user_id", "provider", "operator_key")
                .annotate(total=Count("pk"))
                .filter(total__gt=1)
            )
            if duplicate_users:
                blockers.append("обнаружены дубли привязок сотрудника")

            inconsistent_tokens = StaffMessengerPairingToken.objects.filter(
                used_at__isnull=False, revoked_at__isnull=False
            ).count()
            if inconsistent_tokens:
                blockers.append(f"несогласованных pairing-token: {inconsistent_tokens}")
            stale_tokens = StaffMessengerPairingToken.objects.filter(
                used_at__isnull=True, revoked_at__isnull=True, expires_at__lte=now
            ).count()
            if stale_tokens:
                warnings.append(f"истёкших неиспользованных pairing-token: {stale_tokens}")

            for label, runtime in (("Telegram", TelegramBotRuntime), ("MAX", MaxBotRuntime)):
                row = runtime.objects.filter(pk=runtime.SINGLETON_PK).first()
                fresh = row and row.heartbeat_at and now - row.heartbeat_at <= HEARTBEAT_MAX_AGE
                if fresh:
                    self.stdout.write(f"{label}-бот: heartbeat свежий")
                else:
                    warnings.append(f"{label}-бот: нет свежего heartbeat")
        except DatabaseError as exc:
            raise CommandError(f"database unavailable ({type(exc).__name__})") from exc

        for warning in warnings:
            self.stdout.write(self.style.WARNING("ПРЕДУПРЕЖДЕНИЕ: " + warning))
        if blockers:
            raise CommandError("АКТИВАЦИЯ ЗАБЛОКИРОВАНА: " + "; ".join(blockers))
        self.stdout.write(self.style.SUCCESS("Готовность проверена: критичных блокеров нет."))
