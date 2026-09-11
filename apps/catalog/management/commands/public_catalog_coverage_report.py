"""Print the read-only content coverage of the public catalog."""

import json

from django.core.management.base import BaseCommand

from apps.catalog.public_coverage import collect_coverage


def _share(part: int, whole: int) -> str:
    return f"{part:>9} ({part / whole:6.1%})" if whole else f"{part:>9}"


class Command(BaseCommand):
    help = "Read-only report: how complete the public catalog is for customers."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Machine-readable output.")

    def handle(self, *args, **options):
        report = collect_coverage()
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return
        whole = report.public_parts
        rows = [
            ("Всего карточек", f"{report.total_parts:>9}"),
            ("Публичных (видны покупателю)", f"{whole:>9}"),
            ("Скрыты из каталога", f"{report.hidden_parts:>9}"),
            ("Отключены (архив)", f"{report.retired_parts:>9}"),
            ("С подтверждённым русским названием", _share(report.with_confirmed_ru, whole)),
            ("Без подтверждённого русского названия", _share(report.without_confirmed_ru, whole)),
            ("С опубликованным фото", _share(report.with_published_photo, whole)),
            ("Без фото", _share(report.without_photo, whole)),
            ("С подтверждёнными аналогами", _share(report.with_confirmed_analogs, whole)),
            ("  из них оригиналы", f"{report.confirmed_originals:>9}"),
            ("  из них аналоги", f"{report.confirmed_analogs:>9}"),
            ("В наличии", _share(report.in_stock, whole)),
            ("Нет на складе", _share(report.zero_stock, whole)),
            ("Цена известна", _share(report.price_known, whole)),
            ("Цена не указана (Уточнить цену)", _share(report.price_unknown, whole)),
            ("С артикулом", _share(report.with_article, whole)),
            ("Без артикула", _share(report.without_article, whole)),
            ("С производителем", _share(report.with_manufacturer, whole)),
            ("Без производителя", _share(report.without_manufacturer, whole)),
            ("Без области применения", _share(report.without_application, whole)),
        ]
        width = max(len(label) for label, _value in rows)
        for label, value in rows:
            self.stdout.write(f"{label:<{width}}  {value}")
        self.stdout.write("")
        self.stdout.write("Область применения (явно указана оператором):")
        for value, count in report.applications.items():
            self.stdout.write(f"  {value:<{width - 2}}  {_share(count, whole)}")
        self.stdout.write("")
        self.stdout.write("Производители (крупнейшие):")
        for name, count in report.top_manufacturers:
            self.stdout.write(f"  {name[: width - 2]:<{width - 2}}  {_share(count, whole)}")
