"""Customer-price protection tied to the actual inventory source."""

from decimal import Decimal


def resolve_effective_inventory_customer_price(inventory, current_price):
    """Return the higher valid current or receipt customer price, else ``None``.

    ``inventory`` is a StockLot or PartItem.  Receipt values are immutable
    historical snapshots and intentionally are not derived from landed cost.
    """
    snapshot = inventory.receipt_customer_price_rub
    # Zero is an explicit, existing canonical price and must not be confused
    # with unknown (NULL).  ``check_sale_line_price`` keeps its established
    # source-specific guard for that case.
    valid = [
        value for value in (current_price, snapshot) if isinstance(value, Decimal) and value >= 0
    ]
    return max(valid) if valid else None
