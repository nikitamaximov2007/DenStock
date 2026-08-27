"""Recalculate current catalog recommendations from wholesale source prices."""

from django.core.management.base import BaseCommand

from apps.catalog.services import (
    get_current_price_settings,
    plan_linked_part_price_refresh,
    refresh_linked_part_prices,
)


class Command(BaseCommand):
    help = (
        "Пересчитать текущие рекомендованные цены BRP/Polaris/аналогов из "
        "оптовых цен. По умолчанию только dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Применить только рассчитанные изменения PartType.recommended_price.",
        )
        parser.add_argument(
            "--catalog",
            choices=("all", "brp", "polaris", "aftermarket"),
            default="all",
            help="Ограничить пересчёт одним каталогом (по умолчанию все).",
        )

    def handle(self, *args, **options):
        catalogs = (
            frozenset({"brp", "polaris", "aftermarket"})
            if options["catalog"] == "all"
            else frozenset({options["catalog"]})
        )
        pricing = get_current_price_settings(create=False)
        plan = plan_linked_part_price_refresh(
            usd_rate=pricing.current_usd_rate,
            brp_markup=pricing.brp_markup_percent,
            polaris_markup=pricing.polaris_markup_percent,
            catalogs=catalogs,
        )

        write = self.stdout.write
        write("Текущие рекомендованные цены из оптовых цен")
        write("Режим: ПРИМЕНЕНИЕ" if options["apply"] else "Режим: DRY-RUN")
        write(f"Курс USD: {pricing.current_usd_rate}")
        write(f"Наценка BRP: {pricing.brp_markup_percent}%")
        write(f"Наценка Polaris: {pricing.polaris_markup_percent}%")
        write(f"Наценка для аналогов: {pricing.brp_markup_percent}% (та же, что у BRP)")
        write(f"Расчётных BRP-связей: {plan.brp_links}")
        write(f"Расчётных Polaris-связей: {plan.polaris_links}")
        write(f"Карточек каталога аналогов: {plan.aftermarket_links}")
        write(f"Ручных цен пропущено: {plan.skipped_manual}")
        write(f"Без оптовой цены, текущая цена сохранена: {plan.skipped_without_wholesale}")
        write(f"Без изменения: {plan.unchanged}")
        write(f"К изменению рекомендованных цен: {plan.updated}")

        if not options["apply"]:
            write("Dry-run: PartType, link snapshots, продажи и склад не изменялись.")
            return

        updated = refresh_linked_part_prices(
            usd_rate=pricing.current_usd_rate,
            brp_markup=pricing.brp_markup_percent,
            polaris_markup=pricing.polaris_markup_percent,
            catalogs=catalogs,
        )
        write(self.style.SUCCESS(f"Обновлено рекомендованных цен: {updated}"))
