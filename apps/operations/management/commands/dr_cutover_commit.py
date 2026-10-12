from apps.operations.offline_cutover import COMMIT_PHRASE, commit_cutover

from ._dr_cutover_common import DRCommand


class Command(DRCommand):
    help = (
        "Offline DR: зафиксировать проверенное новое поколение после POST-бэкапа "
        "(COMMITTED). Старое поколение не удаляется."
    )

    def add_arguments(self, parser):
        self.add_journal_argument(parser)
        parser.add_argument("--post-backup", required=True, help="Каталог POST-бэкапа")
        parser.add_argument("--confirm", required=True, help=f"Фраза {COMMIT_PHRASE}")

    def handle(self, *args, **options):
        result = self.run_service(
            commit_cutover, options["stage_journal"],
            post_backup=options["post_backup"], confirm=options["confirm"],
        )
        retained = result["retained"]
        self.stdout.write(self.style.SUCCESS(
            f"COMMITTED (POST-бэкап {result['post_backup_run_id']}). Старое поколение "
            f"сохранено: база {retained['database']}, {retained['media']}, "
            f"{retained['private_media']}."
        ))
