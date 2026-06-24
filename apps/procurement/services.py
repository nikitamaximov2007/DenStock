"""Слой 7 — расчёт и фиксация landed cost.

Распределяет накладные расходы партии (`total_extra_cost`) по строкам и
замораживает себестоимость. Физический остаток на складе при этом не
создаётся — это слои 8–12.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Batch, money


class LandedCostError(Exception):
    """Невозможно рассчитать или зафиксировать себестоимость партии."""


def compute_landed_cost(batch: Batch) -> dict:
    """Чистый расчёт распределения накладных без сохранения.

    Возвращает словарь:
        {"method": фактически применённый метод,
         "extra": накладные ₽,
         "lines": [{"line", "allocated", "landed_total", "landed_unit"}, ...]}

    Бросает LandedCostError, если в партии нет строк или распределять не на что.
    """
    lines = list(batch.lines.all())
    if not lines:
        raise LandedCostError("В партии нет строк — нечего рассчитывать.")

    extra = money(batch.total_extra_cost)
    method = batch.cost_allocation_method

    total_value = sum((line.total_cost_rub for line in lines), Decimal("0"))
    total_qty = sum((line.quantity for line in lines), Decimal("0"))

    # by_value по умолчанию; если суммарная стоимость нулевая (например,
    # бесплатные образцы с оплаченной доставкой) — запасной вес по количеству.
    if method == Batch.AllocationMethod.BY_VALUE and total_value == 0:
        method = Batch.AllocationMethod.BY_QUANTITY

    if method == Batch.AllocationMethod.BY_VALUE:
        weights = [line.total_cost_rub for line in lines]
        total_weight = total_value
    else:
        weights = [line.quantity for line in lines]
        total_weight = total_qty

    if total_weight == 0:
        raise LandedCostError(
            "Невозможно распределить расходы: нет ни стоимости, ни количества."
        )

    # Распределение с округлением до копеек.
    if extra > 0:
        allocations = [money(extra * w / total_weight) for w in weights]
        # Копеечная разница после округления добирается на крупнейшую строку
        # (по total_cost_rub), чтобы Σ долей точно сошлась с накладными.
        diff = extra - sum(allocations, Decimal("0"))
        if diff != 0:
            largest = max(range(len(lines)), key=lambda i: lines[i].total_cost_rub)
            allocations[largest] = money(allocations[largest] + diff)
    else:
        allocations = [Decimal("0.00") for _ in lines]

    result = []
    for line, allocated in zip(lines, allocations, strict=True):
        landed_total = money(line.total_cost_rub + allocated)
        landed_unit = money(landed_total / line.quantity) if line.quantity else Decimal("0.00")
        result.append(
            {
                "line": line,
                "allocated": allocated,
                "landed_total": landed_total,
                "landed_unit": landed_unit,
            }
        )
    return {"method": method, "extra": extra, "lines": result}


@transaction.atomic
def finalize_cost(batch: Batch, user) -> Batch:
    """Зафиксировать себестоимость: рассчитать, записать в строки, закрыть.

    В одной транзакции под блокировкой партии: проверяет статус и
    единственность фиксации, пишет landed-поля строк, переводит
    `accepted → cost_calculated`, выставляет `cost_finalized`.
    Повторная фиксация запрещена.
    """
    batch = Batch.objects.select_for_update().get(pk=batch.pk)
    if batch.status != Batch.Status.ACCEPTED:
        raise LandedCostError("Рассчитать себестоимость можно только для принятой партии.")
    if batch.cost_finalized:
        raise LandedCostError("Себестоимость уже зафиксирована.")

    computed = compute_landed_cost(batch)
    for row in computed["lines"]:
        line = row["line"]
        line.allocated_overhead_rub = row["allocated"]
        line.landed_total_cost_rub = row["landed_total"]
        line.landed_unit_cost_rub = row["landed_unit"]
        line.save(
            update_fields=[
                "allocated_overhead_rub",
                "landed_total_cost_rub",
                "landed_unit_cost_rub",
                "updated_at",
            ]
        )

    batch.cost_allocation_method = computed["method"]
    batch.status = Batch.Status.COST_CALCULATED
    batch.cost_finalized = True
    batch.cost_finalized_at = timezone.now()
    batch.cost_finalized_by = user
    batch.save(
        update_fields=[
            "cost_allocation_method",
            "status",
            "cost_finalized",
            "cost_finalized_at",
            "cost_finalized_by",
            "updated_at",
        ]
    )
    return batch
