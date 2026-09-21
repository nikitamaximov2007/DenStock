from django.core.management.base import BaseCommand, CommandError

from apps.catalog_import.bearing_catalog import (
    PRICE_SEMANTICS_CUSTOMER,
    apply_plan,
    build_plan,
)


class Command(BaseCommand):
    help = (
        "Проверить список подшипников или явно применить его как клиентские цены. "
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
            help="Подтвердить, что цены из источника являются ценами для клиента.",
        )

    def handle(self, *args, **options):
        semantics = (
            PRICE_SEMANTICS_CUSTOMER
            if options["prices_are_customer_selling"]
            else None
        )
        plan = build_plan(price_semantics=semantics or "unconfirmed")
        summary = plan.as_summary()
        self.stdout.write(f"Строк: {summary['rows']}")
        self.stdout.write(f"CREATE: {summary['CREATE']}")
        self.stdout.write(f"ALREADY_EXISTS: {summary['ALREADY_EXISTS']}")
        self.stdout.write(f"AMBIGUOUS: {summary['AMBIGUOUS']}")
        self.stdout.write(f"Штрихкоды: {summary['barcodes_created']}")
        self.stdout.write("Остатки: 0")
        if not options["apply"]:
            self.stdout.write(
                "Режим проверки: изменений нет. Смысл RUB-цен пока не подтверждён."
            )
            return
        if not options["prices_are_customer_selling"]:
            raise CommandError(
                "Импорт остановлен: подтвердите --prices-are-customer-selling "
                "только если источник содержит цены для клиента."
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
