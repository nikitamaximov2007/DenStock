"""Цена клиента, привязанная к фактическому складскому источнику.

Снимок появляется только у нового поступления, когда склад реально принимает
деталь.  Он не является себестоимостью и не пытается восстановить цену для
старого остатка: отсутствие снимка остаётся отсутствием исторических данных.
"""

from decimal import Decimal


def resolve_effective_inventory_customer_price(inventory, current_price) -> Decimal | None:
    """Return the safe default for one item or lot.

    The current canonical ``PartType.recommended_price`` and the immutable
    receipt snapshot are independent candidates.  A manual document price is
    intentionally handled by its caller and never passes through this helper.
    """
    values = [
        value
        for value in (
            current_price,
            getattr(inventory, "receipt_customer_price_snapshot_rub", None),
        )
        if value is not None
    ]
    return max(values) if values else None
