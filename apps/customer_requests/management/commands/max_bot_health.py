"""Container healthcheck of max-bot: heartbeat fresh and the lease its own.

Prints one line, ``ok`` or ``unhealthy: <reasons>``. Makes no MAX call, reads no
secret and never prints configuration.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.customer_requests.max_bot import health_problems


class Command(BaseCommand):
    help = "Проверка здоровья MAX-бота для healthcheck контейнера."

    def handle(self, *args, **options):
        try:
            problems = health_problems(settings.MAX_BOT_HEARTBEAT_FILE)
        except DatabaseError as exc:
            problems = [f"database unavailable ({type(exc).__name__})"]
        if problems:
            raise CommandError("unhealthy: " + "; ".join(problems))
        self.stdout.write("ok")
