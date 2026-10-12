from apps.operations.offline_cutover import RETIRE_PHRASE, retire_cutover

from ._dr_cutover_common import DRCommand


class Command(DRCommand):
    help = (
        "Offline DR: снять метку поколения с рабочей базы после фиксации (RETIRED), "
        "чтобы будущий перенос томов не блокировал запуск. Старая база остаётся "
        "помеченной и не обслуживается."
    )

    def add_arguments(self, parser):
        self.add_journal_argument(parser)
        parser.add_argument("--confirm", required=True, help=f"Фраза {RETIRE_PHRASE}")

    def handle(self, *args, **options):
        result = self.run_service(
            retire_cutover, options["stage_journal"], confirm=options["confirm"],
        )
        self.stdout.write(self.style.SUCCESS(
            f"RETIRED: метка снята с базы {result['database']}."
        ))
