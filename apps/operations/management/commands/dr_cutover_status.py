from django.core.management.base import CommandError

from apps.operations.offline_cutover import cutover_status

from ._dr_cutover_common import DRCommand


class Command(DRCommand):
    help = (
        "Offline DR: только чтение. Состояние staging и переключения, фактические "
        "базы и каталоги, и можно ли запускать приложение с текущими настройками. "
        "Код выхода не 0, если запуск небезопасен."
    )

    def add_arguments(self, parser):
        parser.add_argument("stage_journal", nargs="?", help="Журнал dr_stage_restore")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        report = self.run_service(cutover_status, options["stage_journal"])
        if options["json"]:
            self.write_json(report)
        else:
            keys = ("stage_state", "cutover_state", "cutover_id", "reality", "slots",
                    "reality_detail", "lock_holder")
            for key in keys:
                if key in report:
                    self.stdout.write(f"{key}: {report[key]}")
            for problem in report["problems"]:
                self.stdout.write(self.style.ERROR(f"проблема: {problem}"))
        if not report["safe_to_serve"]:
            raise CommandError("Запуск приложения небезопасен (см. проблемы выше).")
        self.stdout.write(self.style.SUCCESS("Запуск приложения с текущими настройками безопасен."))
