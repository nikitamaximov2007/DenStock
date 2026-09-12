"""Сверить клиентские цены с оптовыми ценами каталога. Только чтение."""

import csv
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.price_audit import (
    CATEGORIES,
    PRICE_MISMATCH,
    audit_prices,
)
from apps.catalog.services import get_current_price_settings


class Command(BaseCommand):
    help = (
        "Проверить, что клиентская цена каждой детали равна оптовой цене "
        "каталога, пересчитанной по курсу и наценке. Ничего не изменяет."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--rate",
            help="Курс ₽/$ для проверки (по умолчанию текущий из настроек цен).",
        )
        parser.add_argument(
            "--markup",
            help="Наценка %% для обоих каталогов (по умолчанию текущие настройки).",
        )
        parser.add_argument(
            "--csv",
            help="Куда выгрузить подробные строки отчёта (CSV, UTF-8).",
        )
        parser.add_argument(
            "--show",
            type=int,
            default=25,
            help="Сколько расхождений напечатать подробно (0 - не печатать).",
        )

    def handle(self, *args, **options):
        pricing = get_current_price_settings(create=False)
        rate = _decimal(options["rate"], pricing.current_usd_rate, "--rate")
        brp_markup = _decimal(options["markup"], pricing.brp_markup_percent, "--markup")
        polaris_markup = _decimal(options["markup"], pricing.polaris_markup_percent, "--markup")

        report = audit_prices(
            usd_rate=rate, brp_markup=brp_markup, polaris_markup=polaris_markup
        )

        write = self.stdout.write
        write("Аудит клиентских цен: только чтение, ни одна цена не изменена")
        write(f"Курс: {rate} ₽/$   наценка BRP: {brp_markup}%   Polaris: {polaris_markup}%")
        write("Формула: оптовая USD × курс × (1 + наценка/100), до целого рубля ROUND_HALF_UP")
        write("")
        write(f"Всего проверено деталей: {report.audited}")
        for name in CATEGORIES:
            write(f"  {name}: {report.by_category.get(name, 0)}")
        write("")
        write("По источнику оптовой цены:")
        for source, count in sorted(report.by_source.items()):
            write(f"  {source}: {count}")
        write("")
        write(f"Публичных деталей с ценой: {report.public_with_price}")
        for name in CATEGORIES:
            count = report.public_by_category.get(name, 0)
            if count:
                write(f"  {name}: {count}")
        write(f"Из них в наличии: {report.in_stock_with_price}")
        for name in CATEGORIES:
            count = report.in_stock_by_category.get(name, 0)
            if count:
                write(f"  {name}: {count}")
        write("")
        unverifiable = report.priced_without_verifiable_source
        write(f"Цена показывается, а источник не проверяем: {unverifiable}")
        write(f"Деталей с несколькими источниками цены: {report.parts_with_several_sources}")
        write(f"Расхождений в наличии: {len(report.in_stock_mismatches)}")

        if options["show"]:
            self._print_mismatches(report, options["show"])
        if options["csv"]:
            _write_csv(options["csv"], report)
            write(f"Подробности: {options['csv']} ({len(report.rows)} строк)")

    def _print_mismatches(self, report, limit):
        rows = report.in_stock_mismatches + [
            row for row in report.mismatches if row.available <= 0
        ]
        if not rows:
            self.stdout.write(self.style.SUCCESS("\nРасхождений цены нет."))
            return
        self.stdout.write(f"\nРасхождения ({PRICE_MISMATCH}), первые {min(limit, len(rows))}:")
        for row in rows[:limit]:
            self.stdout.write(
                f"  #{row.part_id} {row.article or '-'} [{row.manufacturer or '-'}] "
                f"{row.name[:40]}\n"
                f"      источник {row.source}:{row.source_reference} "
                f"опт {row.wholesale_usd} $"
                + (f" (+{row.surcharge_usd} $ VIN)" if row.surcharge_usd else "")
                + f"\n      стоит {row.actual_price} ₽, ожидается {row.expected_price} ₽, "
                f"разница {row.delta} ₽ ({row.delta_percent}%)"
                + (f", остаток {row.available}" if row.available else "")
            )


def _decimal(raw, default, flag):
    if raw in (None, ""):
        return default
    try:
        return Decimal(str(raw))
    except ArithmeticError as exc:
        raise CommandError(f"{flag}: ожидается число, получено {raw!r}") from exc


FIELDS = (
    "part_id",
    "category",
    "article",
    "name",
    "manufacturer",
    "source",
    "source_reference",
    "wholesale_usd",
    "surcharge_usd",
    "actual_price",
    "expected_price",
    "delta",
    "delta_percent",
    "manual",
    "is_public",
    "available",
    "reason",
)


def _write_csv(path, report):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        for row in report.rows:
            writer.writerow([getattr(row, name) for name in FIELDS])
