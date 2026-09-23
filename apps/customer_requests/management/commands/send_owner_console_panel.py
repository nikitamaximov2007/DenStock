"""Queue one owner console panel for each already-bound owner identity."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.customer_requests import operator_console
from apps.customer_requests.models import StaffMessengerBinding

OWNER_LABELS = tuple(operator_console.OWNER_OPERATOR_KEYS)


class Command(BaseCommand):
    help = "Поставить панель владельца PRO-STORE в очереди Telegram и MAX."

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            choices=[
                "all",
                StaffMessengerBinding.Provider.TELEGRAM,
                StaffMessengerBinding.Provider.MAX,
            ],
            default="all",
            help="Канал доставки: telegram, max или all.",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Повторно поставить уже доставленную панель в очередь.",
        )
        parser.add_argument(
            "--operator-key",
            choices=[*OWNER_LABELS, "NIKITA"],
            help="Ограничить отправку одной durable identity.",
        )

    def handle(self, *args, **options):
        if not operator_console.enabled():
            raise CommandError(
                "CUSTOMER_OPERATOR_CONSOLE_ENABLED=false: панель владельца не отправлена."
            )

        provider = options["provider"]
        providers = (
            [StaffMessengerBinding.Provider.TELEGRAM, StaffMessengerBinding.Provider.MAX]
            if provider == "all"
            else [provider]
        )
        filters = {
            "provider__in": providers,
            "is_active": True,
            "user__is_active": True,
        }
        if options["operator_key"] == "NIKITA":
            filters["operator_key"] = "NIKITA"
        elif options["operator_key"]:
            filters["customer_visible_label"] = options["operator_key"]
        else:
            filters["customer_visible_label__in"] = OWNER_LABELS
        bindings = StaffMessengerBinding.objects.select_related("user").filter(
            **filters
        ).order_by("customer_visible_label", "provider", "pk")
        queued = refreshed = existing = 0
        for binding in bindings:
            row, created = operator_console.queue_owner_panel(
                binding=binding, refresh=options["refresh"]
            )
            if row is None:
                continue
            if created:
                queued += 1
            elif options["refresh"]:
                refreshed += 1
            else:
                existing += 1

        self.stdout.write(
            f"Панелей поставлено в очередь: {queued}; "
            f"обновлено: {refreshed}; уже существуют: {existing}."
        )
