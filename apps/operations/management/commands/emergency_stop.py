from django.core.management.base import BaseCommand, CommandError

from apps.operations.failback import FailbackError, freeze_and_export

CONFIRM_PHRASE = "ЗАВЕРШИТЬ-И-ЗАМОРОЗИТЬ"


class Command(BaseCommand):
    help = "Заморозить local emergency instance и создать verified final export."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", required=True)
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Продолжить export после подтверждённого прерывания процесса.",
        )

    def handle(self, *args, **options):
        if options["confirm"] != CONFIRM_PHRASE:
            raise CommandError(f"Для подтверждения укажите --confirm {CONFIRM_PHRASE}")
        try:
            session = freeze_and_export(resume=options["resume"])
        except FailbackError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Offline session заморожена. Final backup: {session.final_backup_run_id}. "
                "Новые складские записи запрещены."
            )
        )
