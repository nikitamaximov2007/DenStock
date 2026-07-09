"""Безопасная отмена ошибочной ПРОДАЖИ со склада (дублирующая продажа и т.п.).

    python manage.py cancel_warehouse_action --action-id ID --reason "..." --dry-run
    python manage.py cancel_warehouse_action --action-id ID --reason "..." --commit

Возвращает остаток в ту же ячейку (движение возврата), сторнирует продажу и
помечает действие отменённым. Отменённое действие не входит в итоги отчёта,
таможенный блок и Excel, но остаётся в аудите. Без --commit — только показ.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.actions.models import WarehouseAction
from apps.actions.services import ActionError, cancel_warehouse_action


class Command(BaseCommand):
    help = "Отменить продажу (WarehouseAction типа sale): вернуть остаток и сторнировать."

    def add_arguments(self, parser):
        parser.add_argument("--action-id", type=int, required=True, help="ID действия (продажи)")
        parser.add_argument("--reason", required=True, help="Причина отмены (обязательно)")
        parser.add_argument("--commit", action="store_true", help="Выполнить (без флага: dry-run)")
        parser.add_argument("--dry-run", action="store_true", help="Явный dry-run (по умолчанию)")

    def handle(self, *args, **options):
        if options["commit"] and options["dry_run"]:
            raise CommandError("Выберите одно: --dry-run ИЛИ --commit.")
        action = (
            WarehouseAction.objects.filter(pk=options["action_id"])
            .select_related("part_type", "location", "sale")
            .first()
        )
        if action is None:
            raise CommandError(f"Действие {options['action_id']} не найдено.")
        write = self.stdout.write
        mode = "ВЫПОЛНЕНО (--commit)" if options["commit"] else "DRY-RUN (ничего не изменено)"
        write(f"Режим: {mode}")
        write(f"Действие id={action.pk}: {action.get_action_type_display()} / {action.status}")
        write(f"  номер детали: {action.part_number or action.part_type.name}")
        write(f"  ячейка: {action.location_code or action.location.code}")
        write(f"  количество: {action.quantity}")
        write(f"  сумма: {action.total_price_rub} ₽")
        if action.sale_id:
            write(f"  продажа: {action.sale.number} (статус {action.sale.status})")
            for line in action.sale.lines.select_related("stock_lot").all():
                write(
                    f"    вернётся в лот #{getattr(line.stock_lot, 'pk', '?')}: "
                    f"{line.quantity} шт в ячейку {action.location.code}"
                )
        if action.action_type != WarehouseAction.Type.SALE:
            write("Это не продажа: отмена этой командой не поддерживается.")
            return
        if action.status == WarehouseAction.Status.CANCELLED:
            write("Действие уже отменено: менять нечего.")
            return
        if not options["commit"]:
            write("Будет: возврат остатка в ячейку, сторно продажи, пометка отменено.")
            return
        try:
            cancel_warehouse_action(action, reason=options["reason"])
        except ActionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            "Продажа отменена: остаток возвращён, документ сторнирован."
        ))
