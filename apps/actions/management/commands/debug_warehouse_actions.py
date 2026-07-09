"""Диагностика действий со склада по номеру детали (подмена номера).

    python manage.py debug_warehouse_actions --material-no 420931285

Показывает по каждому действию: отображаемый номер (snapshot), номер
карточки (primary/OEM), все PartNumber карточки с флагом primary, источник
цены, связанный документ, лот/ячейку/количество и номер, который уйдёт в
таможенный экспорт. Помогает доказать, где именно номер детали
подменяется на соседний.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.actions.models import WarehouseAction
from apps.actions.services import build_export_rows, identity_number
from apps.catalog.models import PartNumber, normalize_number


class Command(BaseCommand):
    help = "Диагностика WarehouseAction по номеру детали."

    def add_arguments(self, parser):
        parser.add_argument("--material-no", required=True, help="Номер детали для поиска")

    def handle(self, *args, **options):
        raw = options["material_no"].strip()
        norm = normalize_number(raw)
        if not norm:
            raise CommandError("Пустой номер.")
        part_ids = set(
            PartNumber.objects.filter(normalized_value=norm).values_list("part_id", flat=True)
        )
        actions = list(
            WarehouseAction.objects.filter(part_number__icontains=raw).select_related(
                "part_type", "location", "sale"
            )
        ) or list(
            WarehouseAction.objects.filter(part_type_id__in=part_ids).select_related(
                "part_type", "location", "sale"
            )
        )
        write = self.stdout.write
        write(f"Номер: {raw} (норм. {norm})")
        write(f"Карточек с этим номером: {len(part_ids)}")
        if not actions:
            write("Действий не найдено.")
            return
        for a in actions:
            write("-" * 60)
            write(f"WarehouseAction id={a.pk}  тип={a.action_type}  статус={a.status}")
            write(f"  отображаемый номер (snapshot): {a.part_number!r}")
            write(f"  part_type id={a.part_type_id}  name={a.part_type.name!r}")
            write(f"  primary/OEM номер карточки: {identity_number(a.part_type)!r}")
            numbers = PartNumber.objects.filter(part_id=a.part_type_id).order_by(
                "-is_primary", "pk"
            )
            for pn in numbers:
                flag = " [primary]" if pn.is_primary else ""
                write(f"    номер: {pn.value!r} kind={pn.kind}{flag}")
            write(f"  источник цены (замена): {a.price_source_number or 'нет'}")
            write(f"  ячейка snapshot={a.location_code!r} live={a.location.code!r}")
            write(f"  количество={a.quantity}  сумма={a.total_price_rub}")
            if a.sale_id:
                write(f"  продажа: {a.sale.number} статус={a.sale.status}")
                for line in a.sale.lines.select_related("part_type", "stock_lot").all():
                    lot = line.stock_lot
                    write(
                        f"    sale line: part={line.part_type_id} qty={line.quantity} "
                        f"lot={getattr(lot, 'pk', None)} "
                        f"lot_part={getattr(lot, 'part_type_id', None)}"
                    )
            rows = build_export_rows([a])
            if rows:
                write(f"  номер в таможенном экспорте (колонка B): {rows[0]['number']!r}")
