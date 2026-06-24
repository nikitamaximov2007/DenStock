"""Сверить кэш остатков (StockBalance) с первичкой, ничего не меняя.

Печатает расхождения и завершает работу с ненулевым кодом, если они есть —
удобно для CI/крон-проверки «обнаруженные проблемы». Сверку
«StockLot.quantity ↔ Σ дельт движений» добавим, когда весь оборот пойдёт через
движения (после Слоёв 12/16).
"""
import sys

from django.core.management.base import BaseCommand

from apps.inventory.services import check_stock_balance


class Command(BaseCommand):
    help = "Сверить кэш остатков с первичкой; ненулевой код возврата при расхождениях."

    def handle(self, *args, **options):
        problems = check_stock_balance()
        if not problems:
            self.stdout.write(self.style.SUCCESS("Расхождений нет: кэш = первичка."))
            return
        for line in problems:
            self.stdout.write(self.style.ERROR(line))
        self.stderr.write(f"Найдено расхождений: {len(problems)}.")
        sys.exit(1)
