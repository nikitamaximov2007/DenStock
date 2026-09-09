"""Public-safe read model for current available stock totals.

The canonical physical and reservation calculation belongs to
``movement.live_stock_rows``. This module deliberately exposes only an
aggregate by PartType ID, so consumers that do not need warehouse internals do
not receive locations, lots, batches, or serial identities.
"""

from collections.abc import Iterable
from decimal import Decimal

from .movement import live_stock_rows

ZERO = Decimal("0")


def available_totals(part_ids: Iterable[int]) -> dict[int, Decimal]:
    """Return current available quantities for the requested part IDs.

    Every requested ID is present in the result, including parts without
    available stock. Availability semantics are owned by ``live_stock_rows``:
    physical bulk and serial stock are included, while receiving, quarantine,
    and active reservations are excluded as appropriate.

    This is a pure read facade. It neither refreshes stock caches nor writes
    any stock, reservation, or catalog record.
    """
    ids = list(dict.fromkeys(part_id for part_id in part_ids if part_id is not None))
    if not ids:
        return {}

    totals = {part_id: ZERO for part_id in ids}
    for row in live_stock_rows(part_ids=ids):
        totals[row.part_type.pk] += row.available
    return totals
