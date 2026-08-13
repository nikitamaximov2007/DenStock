from django.core.management.base import BaseCommand, CommandError

from apps.operations.failback import FailbackError, prepare_failback_package

CONFIRM_PHRASE = "ПОДГОТОВИТЬ-ПАКЕТ"


class Command(BaseCommand):
    help = "Создать локальный verified пакет без production restore или upload."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != CONFIRM_PHRASE:
            raise CommandError(f"Для подтверждения укажите --confirm {CONFIRM_PHRASE}")
        try:
            package, digest = prepare_failback_package()
        except FailbackError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Пакет: {package}")
        self.stdout.write(f"SHA-256: {digest}")
        self.stdout.write(self.style.WARNING("Production restore и upload не выполнялись."))
