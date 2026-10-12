from apps.operations.offline_cutover import recover_cutover

from ._dr_cutover_common import DRCommand


class Command(DRCommand):
    help = (
        "Offline DR: после сбоя сверить журнал с фактическими базами и каталогами и "
        "довести переключение до конца, если это безопасно, иначе откатить. "
        "--rollback: только откат. Неизвестное состояние: RECOVERY_REQUIRED, без действий."
    )

    def add_arguments(self, parser):
        self.add_journal_argument(parser)
        self.add_writers_argument(parser)
        parser.add_argument("--rollback", action="store_true", help="Вернуть старое поколение")

    def handle(self, *args, **options):
        result = self.run_service(
            recover_cutover, options["stage_journal"],
            writers_stopped=options["writers_stopped"], rollback=options["rollback"],
        )
        self.stdout.write(self.style.SUCCESS(
            f"{result['action']}: состояние {result['state']}. {result.get('reason', '')}".strip()
        ))
