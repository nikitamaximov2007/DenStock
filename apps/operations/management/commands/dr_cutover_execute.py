from apps.operations.offline_cutover import execute_cutover

from ._dr_cutover_common import DRCommand


class Command(DRCommand):
    help = (
        "Offline DR: переключить базу, media и private_media на проверенный staging и "
        "проверить новое поколение (POST_CUTOVER_VERIFIED). При ошибке возвращает "
        "старое поколение."
    )

    def add_arguments(self, parser):
        self.add_journal_argument(parser)
        self.add_writers_argument(parser)

    def handle(self, *args, **options):
        result = self.run_service(
            execute_cutover, options["stage_journal"],
            writers_stopped=options["writers_stopped"],
        )
        self.stdout.write(self.style.SUCCESS(
            f"{result['state']}: новое поколение проверено. Сделайте POST-бэкап "
            "(backup_all) и выполните dr_cutover_commit."
        ))
