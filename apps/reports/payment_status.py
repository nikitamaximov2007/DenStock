"""Снимок клиентской суммы для ручного статуса «Оплатил».

Модуль только читает факты проведённых документов. Он намеренно не включает
названия, себестоимость, время или презентационные поля: только устойчивые
идентификаторы строк, их количества, клиентские цены и действенные возвраты.
"""

from __future__ import annotations

import json
from hashlib import sha256

from django.db.models import Sum

from apps.procurement.models import money
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.repairs.services import repair_customer_line_prices, repair_returned_quantities
from apps.returns.models import StockReturn, StockReturnLine
from apps.sales.models import Sale, SaleLine

from .services import DEC0, period_range


def _decimal(value) -> str:
    return format(value, "f")


def _sale_returned_quantities(lines):
    line_ids = [line.pk for line in lines]
    if not line_ids:
        return {}
    return dict(
        StockReturnLine.objects.filter(
            stock_return__status=StockReturn.Status.COMPLETED,
            source_sale_line_id__in=line_ids,
        )
        .values("source_sale_line_id")
        .annotate(quantity=Sum("quantity"))
        .values_list("source_sale_line_id", "quantity")
    )


def customer_payment_states(*, customer_ids, period) -> dict[int, dict]:
    """Return payable client totals in a bounded number of queries for a page."""
    customer_ids = list({customer_id for customer_id in customer_ids if customer_id})
    if not customer_ids:
        return {}
    sale_lines = list(
        SaleLine.objects.filter(
            sale__customer_id__in=customer_ids,
            sale__status=Sale.Status.COMPLETED,
            **period_range("sale__sold_at", period),
        )
        .select_related("sale")
        .only("id", "sale_id", "quantity", "unit_price", "total_price", "sale__customer_id")
    )
    sale_returns = _sale_returned_quantities(sale_lines)
    repair_lines = list(
        RepairIssueLine.objects.filter(
            repair_order__customer_id__in=customer_ids,
            repair_order__status=RepairOrder.Status.COMPLETED,
            **period_range("repair_order__completed_at", period),
        )
        .select_related("repair_order")
        .only(
            "id",
            "repair_order_id",
            "part_type_id",
            "quantity",
            "customer_unit_price_rub",
            "repair_order__customer_id",
        )
    )
    repair_returns = repair_returned_quantities(repair_lines)
    repair_prices = repair_customer_line_prices(repair_lines)

    states = {
        customer_id: {"amount": DEC0, "facts": [], "unknown": False}
        for customer_id in customer_ids
    }
    for line in sale_lines:
        returned = sale_returns.get(line.pk) or DEC0
        net_quantity = max(line.quantity - returned, DEC0)
        line_amount = money(line.unit_price * net_quantity)
        state = states[line.sale.customer_id]
        state["amount"] += line_amount
        state["facts"].append(
            (
                "sale",
                line.sale_id,
                line.pk,
                _decimal(line.quantity),
                _decimal(net_quantity),
                _decimal(line.unit_price),
            )
        )

    for line in repair_lines:
        returned = repair_returns.get(line.pk) or DEC0
        net_quantity = max(line.quantity - returned, DEC0)
        price = repair_prices[line.pk]
        state = states[line.repair_order.customer_id]
        if net_quantity and price.unit_price_rub is None:
            state["unknown"] = True
        elif price.unit_price_rub is not None:
            state["amount"] += money(price.unit_price_rub * net_quantity)
        state["facts"].append(
            (
                "repair",
                line.repair_order_id,
                line.pk,
                _decimal(line.quantity),
                _decimal(net_quantity),
                price.source,
                None if price.unit_price_rub is None else _decimal(price.unit_price_rub),
            )
        )

    result = {}
    for customer_id, state in states.items():
        # Sorted JSON keeps the digest stable across database ordering and process restarts.
        facts = state["facts"]
        facts.sort()
        payload = json.dumps(facts, ensure_ascii=True, separators=(",", ":"))
        result[customer_id] = {
            "amount": money(state["amount"]),
            "fingerprint": sha256(payload.encode("ascii")).hexdigest(),
            "document_count": len({(fact[0], fact[1]) for fact in facts}),
            "acknowledgeable": not state["unknown"],
            # Подтверждение оплаты живёт в границах периода: у «всего времени»
            # их нет, и подтверждать нечего - следующая же продажа сделала бы
            # такое подтверждение неверным.
            "period_bound": not period.all_time,
        }
    return result


def customer_payment_state(*, customer_id: int, period) -> dict:
    """Return one customer's exact payable total and deterministic digest."""
    return customer_payment_states(customer_ids=[customer_id], period=period)[customer_id]


def payment_statuses_for_rows(*, rows, period) -> dict[int, dict]:
    """Return current acknowledgement status for report rows without N+1 queries."""
    from apps.customers.models import CustomerPeriodPaymentAcknowledgement

    customer_ids = [row["customer_id"] for row in rows if row.get("linked")]
    states = customer_payment_states(customer_ids=customer_ids, period=period)
    active = {}
    if period.all_time:
        return {
            customer_id: {**state, "acknowledgement": None, "paid": False}
            for customer_id, state in states.items()
        }
    acknowledgements = (
        CustomerPeriodPaymentAcknowledgement.objects.filter(
            customer_id__in=customer_ids,
            period_start=period.date_from,
            period_end=period.date_to,
            revoked_at__isnull=True,
        )
        .select_related("acknowledged_by")
        .order_by("customer_id", "-acknowledged_at", "-pk")
    )
    for acknowledgement in acknowledgements:
        active.setdefault(acknowledgement.customer_id, acknowledgement)

    statuses = {}
    for customer_id, state in states.items():
        acknowledgement = active.get(customer_id)
        statuses[customer_id] = {
            **state,
            "acknowledgement": acknowledgement,
            "paid": bool(
                acknowledgement
                and state["acknowledgeable"]
                and acknowledgement.amount_rub == state["amount"]
                and acknowledgement.billable_fingerprint == state["fingerprint"]
            ),
        }
    return statuses
