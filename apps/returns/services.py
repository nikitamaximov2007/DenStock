"""Слой 18 — сервисы возврата на склад (физическое обратное поступление).

Единственная точка изменения документа возврата. `apps.returns` ведёт ДОКУМЕНТ
(шапку, строки, заморозку себестоимости, контроль «не больше проданного/выданного»)
и оркеструет проведение, но физику склада НЕ трогает: возврат остатка
(`PartItem.status`/`StockLot.quantity`, `StockMovement`, `StockBalance`) делают
сервисы `apps.inventory` (`return_part_item`/`return_stock_lot_quantity`).

Это возврат НА СКЛАД, а не возврат ДЕНЕГ: итоги `Sale`/`RepairOrder` и их статус
`completed` не меняются (финансовое сторно — будущий слой).
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.inventory.models import PartItem
from apps.inventory.services import (
    InventoryError,
    return_part_item,
    return_stock_lot_quantity,
)
from apps.procurement.models import money
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.sales.models import Sale, SaleLine

from .models import StockReturn, StockReturnLine


class ReturnError(Exception):
    """Невозможно выполнить операцию с возвратом."""


# --- Источник возврата (полиморфизм SaleLine / RepairIssueLine) --------------


def _source_filter(source_line) -> dict:
    """Kwargs-фильтр строк возврата по строке-источнику (SaleLine/RepairIssueLine)."""
    if isinstance(source_line, SaleLine):
        return {"source_sale_line": source_line}
    return {"source_repair_line": source_line}


def returned_quantity_for(source_line) -> Decimal:
    """Сколько уже возвращено по этой строке-источнику ЗАВЕРШЁННЫМИ возвратами."""
    return (
        StockReturnLine.objects.filter(
            stock_return__status=StockReturn.Status.COMPLETED, **_source_filter(source_line)
        ).aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )


def _draft_qty_in_return(ret, source_line) -> Decimal:
    """Сколько уже намечено по этой строке-источнику в данном черновике возврата."""
    return (
        StockReturnLine.objects.filter(stock_return=ret, **_source_filter(source_line))
        .aggregate(s=Sum("quantity"))["s"]
        or Decimal("0")
    )


def returnable_quantity(source_line, *, draft=None) -> Decimal:
    """Доступно к возврату = продано/выдано − уже возвращено − намечено в черновике."""
    base = source_line.quantity - returned_quantity_for(source_line)
    if draft is not None:
        base -= _draft_qty_in_return(draft, source_line)
    return base


# --- Создание / наполнение возврата ------------------------------------------


def create_return(*, source, reason="", comment="", by=None) -> StockReturn:
    """Создать черновик возврата из проведённой `Sale` или `RepairOrder`."""
    if isinstance(source, Sale):
        if source.status != Sale.Status.COMPLETED:
            raise ReturnError("Возврат возможен только из проведённой продажи.")
        source_type = StockReturn.SourceType.SALE
    elif isinstance(source, RepairOrder):
        if source.status != RepairOrder.Status.COMPLETED:
            raise ReturnError("Возврат возможен только из проведённого ремонтного заказа.")
        source_type = StockReturn.SourceType.REPAIR_ORDER
    else:
        raise ReturnError("Неизвестный источник возврата.")
    return StockReturn.objects.create(
        source_type=source_type, source_id=source.pk,
        reason=(reason or "").strip(), comment=(comment or "").strip(),
        created_by=by, status=StockReturn.Status.DRAFT,
    )


def _ensure_draft(ret: StockReturn) -> None:
    if ret.status != StockReturn.Status.DRAFT:
        raise ReturnError("Возврат уже проведён — изменять состав нельзя.")


def _source_belongs(ret: StockReturn, source_line) -> bool:
    """Принадлежит ли строка-источник документу-источнику этого возврата."""
    if isinstance(source_line, SaleLine):
        return (
            ret.source_type == StockReturn.SourceType.SALE
            and source_line.sale_id == ret.source_id
        )
    return (
        ret.source_type == StockReturn.SourceType.REPAIR_ORDER
        and source_line.repair_order_id == ret.source_id
    )


def _add_line(ret, source_line, quantity, *, to_location, restock_status) -> StockReturnLine:
    """Общая логика добавления строки возврата (источник-агностичная)."""
    if not _source_belongs(ret, source_line):
        raise ReturnError("Строка-источник не относится к этому возврату.")
    if restock_status not in (
        StockReturnLine.RestockStatus.AVAILABLE, StockReturnLine.RestockStatus.QUARANTINE
    ):
        raise ReturnError("Недопустимое состояние возврата.")
    if to_location is None or not to_location.can_hold_stock():
        raise ReturnError("Ячейка возврата не предназначена для хранения остатка.")

    is_item = source_line.part_item_id is not None
    if is_item:
        quantity = Decimal("1")
    else:
        quantity = Decimal(quantity)
        if quantity <= 0:
            raise ReturnError("Количество должно быть больше нуля.")

    available = returnable_quantity(source_line, draft=ret)
    if quantity > available:
        raise ReturnError(
            f"Нельзя вернуть {quantity}: доступно к возврату {available}."
        )

    unit_cost = source_line.unit_cost_rub
    fields = dict(
        stock_return=ret, part_type=source_line.part_type,
        part_item=source_line.part_item, stock_lot=source_line.stock_lot,
        batch=source_line.batch, batch_line=source_line.batch_line,
        quantity=quantity, to_location=to_location, restock_status=restock_status,
        unit_cost_rub=unit_cost, total_cost_rub=money(unit_cost * quantity),
    )
    fields.update(_source_filter(source_line))
    return StockReturnLine.objects.create(**fields)


@transaction.atomic
def add_sale_line_return(ret, sale_line, quantity, *, to_location, restock_status, by=None):
    """Добавить в возврат строку из проданного (`SaleLine`)."""
    ret = StockReturn.objects.select_for_update().get(pk=ret.pk)
    _ensure_draft(ret)
    return _add_line(
        ret, sale_line, quantity, to_location=to_location, restock_status=restock_status
    )


@transaction.atomic
def add_repair_line_return(ret, repair_line, quantity, *, to_location, restock_status, by=None):
    """Добавить в возврат строку из выданного в ремонт (`RepairIssueLine`)."""
    ret = StockReturn.objects.select_for_update().get(pk=ret.pk)
    _ensure_draft(ret)
    return _add_line(
        ret, repair_line, quantity, to_location=to_location, restock_status=restock_status
    )


@transaction.atomic
def remove_return_line(line, *, by=None) -> None:
    """Снять позицию из черновика возврата."""
    line = (
        StockReturnLine.objects.select_for_update().select_related("stock_return").get(pk=line.pk)
    )
    _ensure_draft(line.stock_return)
    line.delete()


# --- Проведение --------------------------------------------------------------


@transaction.atomic
def complete_return(ret, *, by=None) -> StockReturn:
    """Провести возврат: вернуть остаток через inventory.return_*, заморозить
    себестоимость. На проведении заново проверяем «не больше проданного/выданного»
    (учитывая завершённые ранее возвраты и накопление по строкам этого документа).
    """
    ret = StockReturn.objects.select_for_update().get(pk=ret.pk)
    if ret.status != StockReturn.Status.DRAFT:
        raise ReturnError("Возврат уже проведён.")
    lines = list(
        ret.lines.select_related(
            "part_item", "stock_lot", "batch_line", "to_location",
            "source_sale_line", "source_repair_line", "part_type",
        )
    )
    if not lines:
        raise ReturnError("Нельзя провести пустой возврат.")

    now = timezone.now()
    processed: dict[tuple[str, int], Decimal] = {}
    for line in lines:
        source = line.source_sale_line or line.source_repair_line
        key = (
            ("sale", source.pk) if line.source_sale_line_id else ("repair", source.pk)
        )
        prior = returned_quantity_for(source) + processed.get(key, Decimal("0"))
        if line.quantity > source.quantity - prior:
            raise ReturnError(
                f"Нельзя вернуть больше проданного/выданного по строке {source.part_type}."
            )
        processed[key] = processed.get(key, Decimal("0")) + line.quantity

        # Заморозка себестоимости из источника (защитно — на случай ручной правки).
        line.unit_cost_rub = source.unit_cost_rub
        line.total_cost_rub = money(line.unit_cost_rub * line.quantity)

        try:
            if line.part_item_id:
                item = PartItem.objects.select_for_update().get(pk=line.part_item_id)
                return_part_item(
                    item, line.to_location, restock_status=line.restock_status,
                    by=by, document_id=ret.pk, comment=f"Возврат {ret.number}",
                )
                line.save(update_fields=["unit_cost_rub", "total_cost_rub"])
            else:
                returned_lot = return_stock_lot_quantity(
                    line.batch_line, line.to_location, line.quantity,
                    unit_cost_rub=line.unit_cost_rub, restock_status=line.restock_status,
                    by=by, document_id=ret.pk, comment=f"Возврат {ret.number}",
                )
                line.returned_lot = returned_lot
                line.save(update_fields=["unit_cost_rub", "total_cost_rub", "returned_lot"])
        except InventoryError as exc:
            # Физический сервис отверг возврат (напр. конфликт статуса лота в ячейке)
            # — поднимаем как доменную ошибку возврата; транзакция откатывается.
            raise ReturnError(str(exc)) from exc

    ret.cost_total = calculate_return_costs(ret)
    ret.status = StockReturn.Status.COMPLETED
    ret.completed_at = now
    ret.save(update_fields=["cost_total", "status", "completed_at", "updated_at"])
    return ret


def calculate_return_costs(ret: StockReturn) -> Decimal:
    """Сумма себестоимости из (замороженных) строк возврата."""
    total = ret.lines.aggregate(s=Sum("total_cost_rub"))["s"] or Decimal("0")
    return money(total)


# --- Чтение для UI: объект-источник и его строки с остатком к возврату --------


def get_source(ret: StockReturn):
    """Объект-источник (Sale/RepairOrder) по шапке возврата, либо None."""
    if ret.source_type == StockReturn.SourceType.SALE:
        return Sale.objects.filter(pk=ret.source_id).first()
    return RepairOrder.objects.filter(pk=ret.source_id).first()


def get_source_lines(source):
    """Строки документа-источника (SaleLine/RepairIssueLine)."""
    return source.lines.select_related("part_type", "part_item", "stock_lot")


def resolve_source_line(ret: StockReturn, source_line_id):
    """Перечитать строку-источник из БД с привязкой к документу возврата (untrusted id)."""
    if ret.source_type == StockReturn.SourceType.SALE:
        return SaleLine.objects.filter(pk=source_line_id, sale_id=ret.source_id).first()
    return RepairIssueLine.objects.filter(
        pk=source_line_id, repair_order_id=ret.source_id
    ).first()
