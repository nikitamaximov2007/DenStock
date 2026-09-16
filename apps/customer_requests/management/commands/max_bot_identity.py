"""Ask MAX who the configured bot is (GET /me). Read-only; never prints a secret.

Prints whether it is a bot, its numeric id, its username (the public nickname
the deep link needs) and the SHA-256 of the CA file the client trusts. With
``--env-line`` it prints only ``MAX_BOT_USERNAME=<username>``, for the release
step that gives catalog-web the public username.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.customer_requests.max_api import MaxError, ca_fingerprints
from apps.customer_requests.max_bot import build_api
from apps.customer_requests.messengers import MAX_USERNAME_RE

CA_HINT = (
    " The MAX certificate chains to the Russian Trusted Root CA: set MAX_API_CA_FILE"
    " to that CA's PEM file (and MAX_API_CA_SHA256 to its fingerprint)."
)


class Command(BaseCommand):
    help = "Проверить бота MAX: GET /me (is_bot, user_id, username). Только чтение."

    def add_arguments(self, parser):
        parser.add_argument("--env-line", action="store_true")

    def handle(self, *args, **options):
        if not settings.MAX_BOT_TOKEN:
            raise CommandError("MAX_BOT_TOKEN не задан.")
        try:
            api = build_api()
            me = api.get_me()
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        except MaxError as exc:
            hint = CA_HINT if "SSLCertVerificationError" in str(exc) else ""
            raise CommandError(f"{exc}.{hint}") from None
        username = str(me.get("username") or "")
        if me.get("is_bot") is not True:
            raise CommandError("MAX answered, but the account is not a bot.")
        if not MAX_USERNAME_RE.fullmatch(username):
            raise CommandError("The bot has no usable public username for a deep link.")
        if options["env_line"]:
            self.stdout.write(f"MAX_BOT_USERNAME={username}")
            return
        self.stdout.write("is_bot: true")
        self.stdout.write(f"user_id: {me.get('user_id')}")
        self.stdout.write(f"username: {username}")
        self.stdout.write(f"deep_link: https://max.ru/{username}?start=...")
        if settings.MAX_API_CA_FILE:
            prints = ", ".join(ca_fingerprints(settings.MAX_API_CA_FILE))
            self.stdout.write(f"ca_file: {settings.MAX_API_CA_FILE} sha256={prints}")
        else:
            self.stdout.write("ca_file: system trust store")
