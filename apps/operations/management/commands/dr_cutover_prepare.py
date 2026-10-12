from apps.operations.offline_cutover import prepare_cutover

from ._dr_cutover_common import DRCommand


class Command(DRCommand):
    help = (
        "Offline DR: повторно проверить staging, убедиться, что writers остановлены, "
        "и поставить метки поколений на обе базы (CUTOVER_PREPARED). Ничего не "
        "переключает; после этого приложение не стартует до execute или recover."
    )

    def add_arguments(self, parser):
        self.add_journal_argument(parser)
        self.add_writers_argument(parser)

    def handle(self, *args, **options):
        result = self.run_service(
            prepare_cutover, options["stage_journal"],
            writers_stopped=options["writers_stopped"],
        )
        plan = result["plan"]
        self.stdout.write(self.style.SUCCESS(
            f"CUTOVER_PREPARED: переключение {plan['cutover_id']}, журнал {result['journal']}. "
            f"База {plan['database']['staged']} станет {plan['database']['live']}, старая "
            f"сохранится как {plan['database']['old']}."
        ))
