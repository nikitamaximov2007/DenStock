"""Создать открывающие движения для остатка, заведённого до ledger (Слои 8–9).

Для каждого PartItem/StockLot без единого движения пишет одно открывающее
receive_*. Идемпотентна (повторный запуск ничего не дублирует) и не трогает
статусы/количества — только пишет историю. Корректный баланс обеспечивает
rebuild_stock_balance, эта команда необязательна.
"""
from django.core.management.base import BaseCommand

from apps.inventory.services import backfill_opening_movements


class Command(BaseCommand):
    help = "Создать открывающие движения для первички без движений (идемпотентно)."

    def handle(self, *args, **options):
        created = backfill_opening_movements()
        self.stdout.write(
            self.style.SUCCESS(f"Создано открывающих движений: {created}.")
        )
