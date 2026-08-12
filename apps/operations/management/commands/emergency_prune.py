from django.core.management.base import BaseCommand, CommandError

from apps.operations.failback import FailbackError, prune_completed_artifacts

CONFIRM_PHRASE = "УДАЛИТЬ-СТАРЫЕ-COMPLETED-КОПИИ"


class Command(BaseCommand):
    help = "Удалить только старые артефакты уже подтверждённых failback sessions."

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int)
        parser.add_argument("--confirm", required=True)

    def handle(self, *args, **options):
        if options["confirm"] != CONFIRM_PHRASE:
            raise CommandError(f"Для подтверждения укажите --confirm {CONFIRM_PHRASE}")
        try:
            removed = prune_completed_artifacts(keep=options["keep"])
        except (FailbackError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Удалено completed artifacts: {len(removed)}")
