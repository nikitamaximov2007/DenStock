"""Inspect or change the MAX webhook subscription. Never runs on its own.

``status`` only reads; ``--require-subscribed`` turns a missing subscription
into a failure, for release gates. ``subscribe`` and ``unsubscribe`` change
what MAX delivers and where, so they require ``--confirm``. Both are safe to
repeat: subscribing again refreshes the same URL, unsubscribing an absent URL
changes nothing.

The webhook secret belongs to web, not to max-bot, so ``subscribe`` can read it
from a mounted env file (``--secret-file``). The token and the secret are never
printed.
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.customer_requests.max_api import MaxError, webhook_secret_is_well_formed
from apps.customer_requests.max_bot import UPDATE_TYPES, build_api


def secret_from_env_file(path: str) -> str:
    """MAX_WEBHOOK_SECRET from a KEY=VALUE file, without echoing anything."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise CommandError("Файл секрета webhook недоступен.") from None
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "MAX_WEBHOOK_SECRET":
            return value.strip().strip("'\"")
    raise CommandError("В файле секрета нет MAX_WEBHOOK_SECRET.")


class Command(BaseCommand):
    help = "Подписка MAX webhook: status | subscribe --confirm | unsubscribe --confirm."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "subscribe", "unsubscribe"])
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--url", default="")
        parser.add_argument("--secret-file", default="")
        parser.add_argument("--require-subscribed", action="store_true")

    def handle(self, *args, **options):
        if not settings.MAX_BOT_TOKEN:
            raise CommandError("MAX_BOT_TOKEN не задан.")
        url = options["url"] or settings.MAX_PUBLIC_WEBHOOK_URL
        action = options["action"]
        if action != "status" and not options["confirm"]:
            raise CommandError("Это изменяет подписку MAX: повторите с --confirm.")
        if action == "subscribe":
            if not url.startswith("https://"):
                raise CommandError("MAX_PUBLIC_WEBHOOK_URL должен быть адресом https://.")
            secret = (
                secret_from_env_file(options["secret_file"])
                if options["secret_file"]
                else settings.MAX_WEBHOOK_SECRET
            )
            if not webhook_secret_is_well_formed(secret):
                raise CommandError("MAX_WEBHOOK_SECRET: 5-256 символов [A-Za-z0-9_-].")
        if action == "unsubscribe" and not url:
            raise CommandError("MAX_PUBLIC_WEBHOOK_URL не задан.")
        try:
            api = build_api()
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        try:
            before = api.list_subscriptions()
            known = {item.get("url") for item in before}
            if action == "subscribe":
                if url in known:
                    self.stdout.write("already subscribed: refreshing the same URL")
                api.subscribe(url=url, secret=secret, update_types=UPDATE_TYPES)
            elif action == "unsubscribe":
                if url in known:
                    api.unsubscribe(url=url)
                else:
                    self.stdout.write("not subscribed: nothing to remove")
            me = api.get_me()
            subscriptions = api.list_subscriptions() if action != "status" else before
        except MaxError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"bot: {me.get('username') or '-'} ({me.get('user_id')})")
        self.stdout.write(f"subscriptions: {len(subscriptions)}")
        for item in subscriptions:
            types = ",".join(item.get("update_types") or [])
            marker = " (this)" if url and item.get("url") == url else " (OTHER)"
            self.stdout.write(f"  {item.get('url')}{marker} [{types}]")
        subscribed = any(item.get("url") == url for item in subscriptions)
        if options["require_subscribed"] and not subscribed:
            raise CommandError("MAX webhook is not subscribed to this URL.")
