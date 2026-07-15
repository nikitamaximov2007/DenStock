from django.core.management.base import BaseCommand, CommandError

from apps.inventory.movement import stock_location_consistency_issues


class Command(BaseCommand):
    help = "Read-only диагностика размещения, кэша остатков и перемещений."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fail-on-issues",
            action="store_true",
            help="Вернуть ненулевой код, если найдены расхождения.",
        )

    def handle(self, *args, **options):
        issues = stock_location_consistency_issues()
        self.stdout.write("Режим: READ ONLY")
        self.stdout.write("Источник текущего остатка: StockLot + PartItem")
        self.stdout.write("История инвентаризации: snapshot, не текущий остаток")
        if issues:
            for issue in issues:
                self.stdout.write(self.style.WARNING(f"ISSUE: {issue}"))
        self.stdout.write(f"ИТОГ: расхождений {len(issues)}")
        if issues and options["fail_on_issues"]:
            raise CommandError("Найдены расхождения складского состояния.")
