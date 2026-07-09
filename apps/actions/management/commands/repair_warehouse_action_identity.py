"""Исправить snapshot номера у исторического WarehouseAction.

    python manage.py repair_warehouse_action_identity --action-id ID \
        --part-number 420931285 --reason "Фактически продан этот номер" --dry-run
    python manage.py repair_warehouse_action_identity --action-id ID \
        --part-number 420931285 --reason "Фактически продан этот номер" --commit

Команда меняет только `WarehouseAction.part_number` (и заполняет пустые
snapshot name/location при необходимости). Остатки, продажи, движения и цены
не изменяются.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.actions.models import WarehouseAction
from apps.actions.services import ActionError, repair_action_identity_snapshot
from apps.catalog.models import normalize_number


class Command(BaseCommand):
    help = "Исправить snapshot номера детали у действия со склада без изменения остатков."

    def add_arguments(self, parser):
        parser.add_argument("--action-id", type=int, required=True, help="ID WarehouseAction")
        parser.add_argument("--part-number", required=True, help="Правильный номер детали")
        parser.add_argument("--reason", required=True, help="Причина исправления")
        parser.add_argument("--commit", action="store_true", help="Выполнить исправление")
        parser.add_argument("--dry-run", action="store_true", help="Явный dry-run (по умолчанию)")

    def handle(self, *args, **options):
        if options["commit"] and options["dry_run"]:
            raise CommandError("Выберите одно: --dry-run ИЛИ --commit.")

        action = (
            WarehouseAction.objects.filter(pk=options["action_id"])
            .select_related("part_type", "location", "sale", "reservation", "repair_order")
            .first()
        )
        if action is None:
            raise CommandError(f"Действие {options['action_id']} не найдено.")

        part_number = options["part_number"].strip()
        if not normalize_number(part_number):
            raise CommandError("Укажите корректный номер детали.")

        write = self.stdout.write
        mode = "ВЫПОЛНЕНО (--commit)" if options["commit"] else "DRY-RUN (ничего не изменено)"
        write(f"Режим: {mode}")
        write(f"Действие id={action.pk}: {action.get_action_type_display()} / {action.status}")
        write(f"  было: {action.part_number or action.part_type.name}")
        write(f"  будет: {part_number}")
        write(f"  карточка: #{action.part_type_id} {action.part_type.name}")
        write(f"  ячейка: {action.location_code or action.location.code}")
        write(f"  количество: {action.quantity}")
        write(f"  сумма: {action.total_price_rub} ₽")
        write(f"  причина: {options['reason'].strip()}")
        if action.sale_id:
            write(f"  продажа: {action.sale.number} (статус {action.sale.status})")
        if action.reservation_id:
            write(f"  резерв: {action.reservation.number} (статус {action.reservation.status})")
        if action.repair_order_id:
            write(f"  ремонт: {action.repair_order.number} (статус {action.repair_order.status})")

        if not options["commit"]:
            write("Будет изменён только snapshot номера действия; остатки и движения не трогаются.")
            return

        try:
            repair_action_identity_snapshot(action, part_number=part_number)
        except ActionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Snapshot номера действия исправлен."))
