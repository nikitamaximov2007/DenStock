"""Layer 34: показать уже проведённые пересчёты ячеек в «Инвентаризации».

    python manage.py migrate_counting_receipts_to_stocktaking --dry-run
    python manage.py migrate_counting_receipts_to_stocktaking --commit

Историческая миграция production: проведённые сессии пересчёта (ячейки
S04-L03-D01-C01..C04) получают IC-номера документов инвентаризации (общий
счётчик с документами сверки) и появляются в разделе /stocktaking/.

Склад НЕ трогается: остатки, лоты, движения и партии уже созданы старым
проведением и не пересоздаются; количество не меняется, дублей нет.
Технические POS-документы физически не удаляются (на них ссылаются
партии/лоты/движения) - из списка «Поступлений» они скрыты фильтром по
связи с сессией. Команда идемпотентна: повторный запуск ничего не меняет.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.counting.models import InventoryCountingSession
from apps.inventory.models import NumberSequence


class Command(BaseCommand):
    help = (
        "Присвоить IC-номера уже проведённым пересчётам ячеек, чтобы они "
        "отображались документами в разделе «Инвентаризация». Склад не меняется."
    )

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true",
                            help="Записать изменения (без флага: dry-run)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Явно указать dry-run (поведение по умолчанию)")

    def handle(self, *args, **options):
        if options["commit"] and options["dry_run"]:
            raise CommandError("Выберите одно: --dry-run ИЛИ --commit.")
        commit = options["commit"]
        write = self.stdout.write
        mode = "ЗАПИСАНО (--commit)" if commit else "DRY-RUN (ничего не записано)"
        write(f"Режим: {mode}")

        posted = list(
            InventoryCountingSession.objects.filter(
                status=InventoryCountingSession.Status.POSTED
            )
            .select_related("converted_receipt")
            .order_by("posted_at", "pk")
        )
        write(f"Найдено проведённых пересчётов: {len(posted)}")

        to_assign = [s for s in posted if not s.inventory_number]
        skipped = len(posted) - len(to_assign)
        hidden = [
            s.converted_receipt.number for s in posted if s.converted_receipt_id
        ]

        for session in posted:
            status = session.inventory_number or "БУДЕТ ПРИСВОЕН"
            receipt = (
                session.converted_receipt.number if session.converted_receipt_id else "нет"
            )
            write(
                f"  {session.full_address}: номер {status}, "
                f"технический документ {receipt}"
            )

        if commit and to_assign:
            with transaction.atomic():
                for session in to_assign:
                    session.inventory_number = NumberSequence.next("inventory_count")
                    session.save(update_fields=["inventory_number", "updated_at"])
                    write(f"  Присвоен {session.inventory_number}: {session.full_address}")

        assigned = len(to_assign) if commit else 0
        write(f"Присвоено номеров: {assigned}"
              + ("" if commit else f" (будет присвоено: {len(to_assign)})"))
        write(f"Пропущено (номер уже есть): {skipped}")
        write(
            "Скрыто из «Поступлений» (фильтром, физически не тронуты): "
            + (", ".join(hidden) or "нет")
        )
        write("Остатки, лоты и движения не менялись.")
        if commit:
            self.stdout.write(self.style.SUCCESS("Миграция завершена."))
