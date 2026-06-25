from django.core.management.base import BaseCommand

from apps.sales.services import expire_reservations


class Command(BaseCommand):
    help = "Перевести просроченные активные резервы в expired и пересобрать кэш остатков."

    def handle(self, *args, **options):
        count = expire_reservations()
        self.stdout.write(self.style.SUCCESS(f"Просрочено резервов: {count}"))
