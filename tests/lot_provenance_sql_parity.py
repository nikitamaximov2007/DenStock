"""Compare the final PostgreSQL audit class with the Python lot classifier."""

import re
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.db import connection

from apps.inventory.lot_provenance import CLASSES, line_provenance_detail
from apps.inventory.models import StockLot
from apps.procurement.models import BatchLine


def assert_final_provenance_parity():
    source = (Path(settings.BASE_DIR) / "docs/audits/lot-provenance-readonly.sql").read_text()
    sql = re.split(r"^-- name: final_provenance\n", source, flags=re.M)[1]
    with connection.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    assert len(rows) == len({row[0] for row in rows}) == StockLot.objects.count()
    sql_classes = {lot_id: provenance for lot_id, _, _, provenance in rows}
    python_classes = {
        lot.lot_id: lot.provenance
        for line in BatchLine.objects.filter(pk__in=StockLot.objects.values("batch_line_id"))
        for lot in line_provenance_detail(line).lots
    }
    assert set(sql_classes.values()) <= set(CLASSES)
    assert set(python_classes) == set(sql_classes)
    mismatches = [
        (lot_id, python_classes[lot_id], sql_classes[lot_id])
        for lot_id in sorted(python_classes)
        if python_classes[lot_id] != sql_classes[lot_id]
    ]
    assert not mismatches, (
        f"Python {dict(Counter(python_classes.values()))}; "
        f"SQL {dict(Counter(sql_classes.values()))}; "
        f"mismatches (lot_id, Python, SQL): {mismatches}"
    )
    return python_classes
