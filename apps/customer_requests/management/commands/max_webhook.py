"""Inspect or change the MAX webhook subscription. Never runs on its own.

``status`` only reads. ``subscribe`` and ``unsubscribe`` change what MAX
delivers and where, so they require ``--confirm``. The secret and the token
are read from settings and never printed.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.customer_requests.max_api import MaxError, webhook_secret_is_well_formed
from apps.customer_requests.max_bot import UPDATE_TYPES, build_api


class Command(BaseCommand):
    help = "Подписка MAX webhook: status | subscribe --confirm | unsubscribe --confirm."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "subscribe", "unsubscribe"])
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not settings.MAX_BOT_TOKEN:
            raise CommandError("MAX_BOT_TOKEN не задан.")
        url = settings.MAX_PUBLIC_WEBHOOK_URL
        action = options["action"]
        if action != "status" and not options["confirm"]:
            raise CommandError("Это изменяет подписку MAX: повторите с --confirm.")
        api = build_api()
        try:
            if action == "subscribe":
                if not url.startswith("https://"):
                    raise CommandError("MAX_PUBLIC_WEBHOOK_URL должен быть адресом https://.")
                if not webhook_secret_is_well_formed(settings.MAX_WEBHOOK_SECRET):
                    raise CommandError("MAX_WEBHOOK_SECRET: 5-256 символов [A-Za-z0-9_-].")
                api.subscribe(url=url, secret=settings.MAX_WEBHOOK_SECRET,
                              update_types=UPDATE_TYPES)
            elif action == "unsubscribe":
                if not url:
                    raise CommandError("MAX_PUBLIC_WEBHOOK_URL не задан.")
                api.unsubscribe(url=url)
            me = api.get_me()
            subscriptions = api.list_subscriptions()
        except MaxError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"bot: {me.get('username') or '-'} ({me.get('user_id')})")
        self.stdout.write(f"subscriptions: {len(subscriptions)}")
        for item in subscriptions:
            types = ",".join(item.get("update_types") or [])
            marker = " (this)" if url and item.get("url") == url else ""
            self.stdout.write(f"  {item.get('url')}{marker} [{types}]")
