from django.core.management.base import BaseCommand, CommandError

from apps.operations.failback import FailbackError, finalize_production_failback

CONFIRM_PHRASE = "ОПАСНО-ПРИНЯТЬ-FAILBACK"


class Command(BaseCommand):
    help = "Post-restore verification and unlock. Database restore не выполняется."

    def add_arguments(self, parser):
        parser.add_argument("--package", required=True)
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != CONFIRM_PHRASE:
            raise CommandError(f"Для подтверждения укажите --confirm {CONFIRM_PHRASE}")
        try:
            session = finalize_production_failback(
                package_path=options["package"], expected_sha256=options["sha256"]
            )
        except FailbackError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Failback session {session.id} проверена. Production writes разрешены."
            )
        )
