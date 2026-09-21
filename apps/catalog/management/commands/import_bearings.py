from django.core.management.base import BaseCommand, CommandError

from apps.catalog_import.bearing_catalog import (
    PRICE_SEMANTICS_PURCHASE,
    apply_plan,
    build_plan,
)


class Command(BaseCommand):
    help = (
        "Проверить список подшипников или явно применить его как закупочные цены. "
        "Остатки и штрихкоды не создаются."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Применить только после явного подтверждения смысла цен.",
        )
        parser.add_argument(
            "--prices-are-customer-selling",
            action="store_true",
            help="Устаревший флаг: клиентская цена не является источником этого импорта.",
        )
        parser.add_argument(
            "--prices-are-purchase-cost",
            action="store_true",
            help="Подтвердить, что цены из источника являются закупочной стоимостью.",
        )

    def handle(self, *args, **options):
        semantics = (
            PRICE_SEMANTICS_PURCHASE if options["prices_are_purchase_cost"] else None
        )
        plan = build_plan(price_semantics=semantics or "unconfirmed")
        summary = plan.as_summary()
        self.stdout.write(f"Строк: {summary['rows']}")
        self.stdout.write(f"CREATE: {summary['CREATE']}")
        self.stdout.write(f"ALREADY_EXISTS: {summary['ALREADY_EXISTS']}")
        self.stdout.write(f"AMBIGUOUS: {summary['AMBIGUOUS']}")
        self.stdout.write(f"Штрихкоды: {summary['barcodes_created']}")
        self.stdout.write("Остатки: 0")
        self.stdout.write("Таблица строк:")
        for row in summary["rows_detail"]:
            self.stdout.write(
                f"{row['brand']} | {row['article']} | "
                f"закупка {row['purchase_price_rub']} ₽ | "
                f"клиент {row['customer_price_rub']} ₽ | {row['status']}"
            )
        if not options["apply"]:
            self.stdout.write(
                "Режим проверки: изменений нет. Смысл RUB-цен пока не подтверждён "
                "как закупочная стоимость."
            )
            return
        if options["prices_are_customer_selling"]:
            raise CommandError(
                "Импорт остановлен: источник содержит закупочную стоимость, "
                "а не цены для клиента."
            )
        if not options["prices_are_purchase_cost"]:
            raise CommandError(
                "Импорт остановлен: подтвердите --prices-are-purchase-cost, "
                "если источник содержит закупочную стоимость."
            )
        try:
            result = apply_plan(plan)
        except Exception as exc:  # noqa: BLE001 - management command boundary
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Создано: {result['created']}; уже было: {result['already_exists']}."
            )
        )
