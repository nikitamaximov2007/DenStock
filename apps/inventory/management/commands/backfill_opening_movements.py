"""Создать открывающие движения для экземпляров, заведённых до ledger (Слой 8).

Для каждого PartItem без единого движения пишет одно открывающее receive_item.
Лоты не трогает: отсутствие движения у лота не доказывает приёмку (см.
apps.inventory.services.backfill_opening_movements и audit_lot_provenance).
Идемпотентна и не меняет статусы/количества.
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
