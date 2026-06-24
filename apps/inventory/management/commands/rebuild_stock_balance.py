"""Полностью пересобрать кэш остатков (StockBalance) из первички.

Кэш не источник истины: остаток считается из StockLot + PartItem. Команда
восстанавливает кэш после миграции/сбоя/ручного вмешательства в БД, а также
наполняет баланс легаси-остатком, заведённым Слоями 8–9 до появления движений.
"""
from django.core.management.base import BaseCommand

from apps.inventory.services import rebuild_stock_balance


class Command(BaseCommand):
    help = "Пересобрать кэш остатков (StockBalance) из StockLot + PartItem."

    def handle(self, *args, **options):
        counts = rebuild_stock_balance()
        self.stdout.write(
            self.style.SUCCESS(
                "Строк баланса: создано {created}, обновлено {updated}, "
                "удалено {deleted}.".format(**counts)
            )
        )
