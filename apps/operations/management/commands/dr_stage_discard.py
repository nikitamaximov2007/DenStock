from django.core.management.base import BaseCommand, CommandError

from apps.operations.offline_restore import StageRestoreError, discard_stage


class Command(BaseCommand):
    help = (
        "Удалить staging-слоты, созданные dr_stage_restore (только то, что записано "
        "в журнале как созданное). Требует --yes."
    )

    def add_arguments(self, parser):
        parser.add_argument("journal", help="Журнал dr_stage_restore")
        parser.add_argument("--yes", action="store_true")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Повторите с --yes, чтобы удалить staging-базу и каталоги.")
        try:
            removed = discard_stage(options["journal"])
        except StageRestoreError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Удалено: " + (", ".join(removed) or "ничего")))
