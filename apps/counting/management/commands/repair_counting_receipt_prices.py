"""Починка цен документа, созданного из пересчёта ячейки (hotfix 33.1).

    python manage.py repair_counting_receipt_prices --session-id 3 --dry-run
    python manage.py repair_counting_receipt_prices --session-id 3 --commit
    python manage.py repair_counting_receipt_prices --receipt-id 3 --commit

До 33.1 конвертация пересчёта писала во все строки документа один глобальный
unit_cost (обычно 0). Команда выставляет строкам документа оценку из строк
пересчёта (final_customer_price_rub), чтобы итог документа совпал со
«Стоимостью ячейки».

Безопасность: чинит ТОЛЬКО документ, связанный с сессией пересчёта (обычные
поступления от поставщика команда не трогает и отказывается чинить);
меняются только цены строк (unit_cost_rub) - количества, ячейки, статусы,
лоты, движения и остатки не затрагиваются; всё в transaction.atomic;
без --commit выполняется dry-run и ничего не пишется.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.counting.models import InventoryCountingSession
from apps.procurement.models import money
from apps.receipts.models import Receipt
from apps.receipts.services import receipt_totals


class Command(BaseCommand):
    help = (
        "Выставить строкам документа из пересчёта ячейки цены клиента "
        "из строк пересчёта (только оценка, склад не меняется)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--session-id", type=int, default=None,
                            help="ID сессии пересчёта (InventoryCountingSession)")
        parser.add_argument("--receipt-id", type=int, default=None,
                            help="ID документа (Receipt), созданного из пересчёта")
        parser.add_argument("--commit", action="store_true",
                            help="Записать изменения (без флага: dry-run)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Явно указать dry-run (поведение по умолчанию)")

    def handle(self, *args, **options):
        if options["commit"] and options["dry_run"]:
            raise CommandError("Выберите одно: --dry-run ИЛИ --commit.")
        session_id, receipt_id = options["session_id"], options["receipt_id"]
        if bool(session_id) == bool(receipt_id):
            raise CommandError("Укажите ровно один параметр: --session-id ИЛИ --receipt-id.")

        if session_id:
            session = InventoryCountingSession.objects.filter(pk=session_id).first()
            if session is None:
                raise CommandError(f"Сессия пересчёта {session_id} не найдена.")
            receipt = session.converted_receipt
            if receipt is None:
                raise CommandError("У этой сессии нет созданного документа.")
        else:
            receipt = Receipt.objects.filter(pk=receipt_id).first()
            if receipt is None:
                raise CommandError(f"Документ {receipt_id} не найден.")
            session = InventoryCountingSession.objects.filter(
                converted_receipt=receipt
            ).first()
            if session is None:
                raise CommandError(
                    "Документ не связан с пересчётом ячейки: обычные поступления "
                    "от поставщика эта команда не чинит."
                )

        commit = options["commit"]
        write = self.stdout.write
        mode = "ЗАПИСАНО (--commit)" if commit else "DRY-RUN (ничего не записано)"
        write(f"Режим: {mode}")
        write(f"Документ: {receipt.number} (id={receipt.pk})")
        write(f"Сессия пересчёта: id={session.pk}, ячейка {session.full_address}")

        counting_lines = list(
            session.lines.select_related("warehouse_part").filter(
                warehouse_part__isnull=False
            )
        )
        by_part: dict[int, list] = {}
        for cline in counting_lines:
            by_part.setdefault(cline.warehouse_part_id, []).append(cline)

        old_totals = receipt_totals(receipt)
        changes = []
        missing = []
        for line in receipt.lines.select_related("part_type").order_by("pk"):
            candidates = by_part.get(line.part_type_id, [])
            match = next(
                (c for c in candidates if c.quantity_counted == line.quantity),
                candidates[0] if candidates else None,
            )
            if match is None:
                missing.append(line)
                continue
            new_price = money(match.final_customer_price_rub or Decimal("0"))
            if new_price == line.unit_cost_rub:
                continue
            changes.append((line, line.unit_cost_rub, new_price))

        if missing:
            for line in missing:
                write(f"  ПРОПУСК: {line.part_type} - нет строки пересчёта, цена не тронута")
        if not changes:
            write("Все цены уже совпадают с пересчётом: менять нечего.")
            return

        new_total = old_totals["cost"]
        write("Строки к обновлению:")
        for line, old_price, new_price in changes:
            old_line_total = money(line.quantity * old_price)
            new_line_total = money(line.quantity * new_price)
            new_total = new_total - old_line_total + new_line_total
            write(
                f"  {line.part_type} x {line.quantity}: "
                f"цена {old_price} -> {new_price} ₽, "
                f"сумма {old_line_total} -> {new_line_total} ₽"
            )
        write(f"Итог документа: {old_totals['cost']} ₽ -> {money(new_total)} ₽")
        write("Количества, остатки, лоты и движения не меняются.")

        if not commit:
            return
        with transaction.atomic():
            for line, _old_price, new_price in changes:
                line.unit_cost_rub = new_price
                line.save(update_fields=["unit_cost_rub"])
        after = receipt_totals(receipt)
        write(self.style.SUCCESS(
            f"Готово: обновлено строк {len(changes)}, итог документа {after['cost']} ₽."
        ))
