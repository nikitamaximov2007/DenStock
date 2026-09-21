"""Safely retain a proven pre-send owner-panel incident for later explicit retry."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.customer_requests import operator_console


class Command(BaseCommand):
    help = "Пометить указанные pre-send панели владельца как требующие явного повтора."

    def add_arguments(self, parser):
        parser.add_argument(
            "--notification-id",
            action="append",
            type=int,
            dest="notification_ids",
            required=True,
            help="ID только проверенного зависшего owner-panel уведомления; можно повторить.",
        )

    def handle(self, *args, **options):
        try:
            count = operator_console.recover_owner_panel_notifications(
                notification_ids=options["notification_ids"]
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Панелей помечено для явного повтора: {count}.")
