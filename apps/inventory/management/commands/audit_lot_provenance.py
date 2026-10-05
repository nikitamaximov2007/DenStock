"""Происхождение каждого лота по журналу движений. Только чтение."""
from collections import Counter
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Count

from apps.inventory.lot_provenance import CLASSES, UNKNOWN, line_provenance
from apps.inventory.models import StockLot
from apps.procurement.models import BatchLine

ACTIVE = {StockLot.Status.RECEIVING, StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE}


class Command(BaseCommand):
    help = (
        "Классифицировать каждый лот по доказательствам журнала (приёмка, перемещение, "
        "возврат, пересчёт, найденное, неизвестно) и показать, какие строки партий "
        "закрыты для приёмки. Ничего не записывает."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--show", type=int, default=50,
            help="Сколько лотов без RECEIVE_LOT напечатать подробно (0 - только сводка).",
        )

    def handle(self, *args, **options):
        write = self.stdout.write
        movement_counts = dict(
            StockLot.objects.annotate(n=Count("movements")).values_list("pk", "n")
        )
        by_class, active_by_class, no_movement_by_class = Counter(), Counter(), Counter()
        unknown_lines, over_received, rows = set(), [], []
        line_ids = StockLot.objects.values_list("batch_line_id", flat=True).distinct()
        for line in BatchLine.objects.filter(pk__in=line_ids).order_by("pk"):
            received, proven = Decimal("0"), True
            for lot in line_provenance(line):
                by_class[lot.provenance] += 1
                if lot.status in ACTIVE:
                    active_by_class[lot.provenance] += 1
                if movement_counts.get(lot.lot_id, 0) == 0:
                    no_movement_by_class[lot.provenance] += 1
                if lot.intake is None:
                    proven = False
                else:
                    received += lot.intake
                if lot.provenance != "primary_receipt":
                    rows.append(lot)
            if not proven:
                unknown_lines.add(line.pk)
            elif received > line.quantity:
                over_received.append((line.pk, line.quantity, received))

        write("Происхождение лотов: только чтение, ничего не записано")
        write(f"Лотов всего: {sum(by_class.values())}")
        write("Класс: всего / активных / без единого движения")
        for name in CLASSES:
            write(
                f"  {name}: {by_class[name]} / {active_by_class[name]} / "
                f"{no_movement_by_class[name]}"
            )
        write(f"Строк партий закрыто для приёмки (есть {UNKNOWN}): {len(unknown_lines)}")
        if unknown_lines:
            write("  " + ", ".join(str(pk) for pk in sorted(unknown_lines)))
        write(f"Строк, где доказанная приёмка больше количества строки: {len(over_received)}")
        for pk, expected, received in over_received:
            write(f"  строка {pk}: количество {expected}, принято {received}")
        show = max(options["show"], 0)
        if show and rows:
            write("")
            write("Лоты без RECEIVE_LOT (лот; строка; статус; класс; учтено; доказательство):")
            for lot in rows[:show]:
                intake = "-" if lot.intake is None else lot.intake
                write(
                    f"  {lot.lot_id}; {lot.batch_line_id}; {lot.status}; {lot.provenance}; "
                    f"{intake}; {lot.evidence}"
                )
            if len(rows) > show:
                write(f"  ... и ещё {len(rows) - show}")
