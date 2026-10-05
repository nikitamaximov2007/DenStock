"""The read-only production SQL for lot provenance agrees with the classifier.

`docs/audits/lot-provenance-readonly.sql` is what an auditor runs against the
production database (the deployed code has no audit command yet). Each query
here runs in a READ ONLY transaction on PostgreSQL 16 over a ledger holding
every evidence case, and must name exactly the lots the classifier names.
"""
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection, transaction

from apps.inventory.lot_provenance import (
    LEGACY_PRIMARY,
    PENDING_RECEIPT,
    PRIMARY_RECEIPT,
    REASSIGNED,
    RETURN_DERIVED,
    TRANSFER_DERIVED,
    UNKNOWN,
    line_provenance_detail,
)
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    create_stock_lot,
    move_stock_lot,
    post_found_stock_group,
    receive_stock_lot,
)
from apps.procurement.models import BatchLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.models import Sale
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.warehouse.models import StorageLocation
from tests.test_lot_provenance_adversarial import (  # noqa: F401
    _age,
    _flip,
    _old_backfill_receipt,
    _transfer,
    env,
)
from tests.test_piece_stock_boundary import _finalized_line, stock  # noqa: F401
from tests.test_piece_stock_boundary_postgresql import units  # noqa: F401

pytestmark = [
    pytest.mark.postgresql,
    # Committed data, so each query runs in its own real READ ONLY transaction.
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL is PostgreSQL"),
]

SQL = Path(settings.BASE_DIR) / "docs" / "audits" / "lot-provenance-readonly.sql"


def _queries():
    parts = re.split(r"^-- name: (\w+)\n", SQL.read_text(encoding="utf-8"), flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _run(name):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(_queries()[name])
        columns = [c.name for c in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def test_the_file_only_reads():
    text = SQL.read_text(encoding="utf-8")
    assert set(_queries()) == {
        "lot_inventory", "transfer_evidence", "reassigned_receipts",
        "old_backfill_receipts", "receipts_over_line", "return_origin_evidence",
    }
    code = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
    assert not re.search(
        r"\b(insert|update|delete|alter|drop|truncate|create|grant|copy)\b", code, re.I
    )
    assert "—" not in text


def test_the_production_queries_name_the_lots_the_classifier_names(units, env):  # noqa: F811
    cells = env["cells"]
    # Primary lot, a transfer out of it, and a false old-backfill receipt on the target.
    primary_line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(primary_line, cells[0], Decimal("10")))
    _transfer(env, "3", cells[0], cells[1], "sql-t1")
    target = StockLot.objects.get(batch_line=primary_line, location=cells[1])
    _old_backfill_receipt(target)
    # A status-flipped legacy lot, a transfer out of it, its target moved on later.
    legacy_line = _finalized_line(env, env["part"], "10")
    _flip(create_stock_lot(legacy_line, cells[2], Decimal("6")))
    _transfer(env, "1", cells[2], cells[3], "sql-t2")
    moved = StockLot.objects.get(batch_line=legacy_line, location=cells[3])
    move_stock_lot(moved, cells[4])
    # A transfer target whose evidence no longer fits (not a transfer: unknown).
    broken_line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(broken_line, cells[3], Decimal("4")))
    _transfer(env, "2", cells[3], cells[5], "sql-t3")
    broken_target = StockLot.objects.get(batch_line=broken_line, location=cells[5])
    _age(broken_target, 60)
    # A target whose line FK is rebound no longer has provable line identity.
    rebound_source_line = _finalized_line(env, env["part"], "5")
    rebound_source_cell = StorageLocation.objects.create(
        name="Rebound source", code="S09-D04-C09", storage_allowed=True, is_active=True
    )
    receive_stock_lot(
        create_stock_lot(rebound_source_line, rebound_source_cell, Decimal("5"))
    )
    rebound_cell = StorageLocation.objects.create(
        name="Rebound target", code="S09-D04-C07", storage_allowed=True, is_active=True
    )
    _transfer(env, "2", rebound_source_cell, rebound_cell, "sql-rebound")
    rebound_target = StockLot.objects.get(batch_line=rebound_source_line, location=rebound_cell)
    rebound_line = _finalized_line(env, env["part"], "5")
    StockLot.objects.filter(pk=rebound_target.pk).update(batch_line=rebound_line)
    # A transfer whose MOVE_LOT product identity disagrees with its source and
    # document is ambiguous, not transfer-derived.
    tampered_line = _finalized_line(env, env["part"], "5")
    tampered_source_cell = StorageLocation.objects.create(
        name="Tampered source", code="S09-D04-C10", storage_allowed=True, is_active=True
    )
    tampered_source = receive_stock_lot(
        create_stock_lot(tampered_line, tampered_source_cell, Decimal("5"))
    )
    tampered_cell = StorageLocation.objects.create(
        name="Tampered target", code="S09-D04-C08", storage_allowed=True, is_active=True
    )
    tampered_transfer = _transfer(
        env, "2", tampered_source_cell, tampered_cell, "sql-tampered-identity"
    )
    tampered_target = StockLot.objects.get(batch_line=tampered_line, location=tampered_cell)
    other_part = env["part"].__class__.objects.create(
        name="Другой артикул", category=env["part"].category, unit=env["part"].unit,
        recommended_price=Decimal("100"), tracking_mode=env["part"].tracking_mode,
    )
    StockMovement.objects.filter(
        document_type="stock_transfer", document_id=tampered_transfer.pk,
        stock_lot=tampered_source,
    ).update(part_type=other_part)
    # A pending lot, a lot re-assigned to another line, and found stock.
    create_stock_lot(_finalized_line(env, env["part"], "5"), cells[5], Decimal("5"))
    reassigned = receive_stock_lot(
        create_stock_lot(_finalized_line(env, env["part"], "3"), cells[1], Decimal("3"))
    )
    StockLot.objects.filter(pk=reassigned.pk).update(batch_line=legacy_line)
    post_found_stock_group(
        entries=[{"source": "warehouse", "source_id": env["part"].pk,
                  "exact_number": "ADV-1", "quantity": 2}],
        location=cells[4], token="sql-found",
    )

    # A new return-created lot has explicit origin evidence. A later return
    # into an old supplier lot must not appear as return origin.
    return_line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(return_line, cells[0], Decimal("10")))
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, source, Decimal("2"), unit_price=Decimal("100"))
    sale = complete_sale(sale, by=env["admin"])
    ret = create_return(source=Sale.objects.get(pk=sale.pk), reason="SQL parity", by=env["admin"])
    add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=cells[1], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(ret, by=env["admin"])
    returned_lot = ret.lines.get().returned_lot
    assert returned_lot.origin_return_line_id == ret.lines.get().pk

    # Existing primary and legacy lots receive later returns. Their origin is
    # unchanged and neither may appear in return-origin SQL.
    reused_primary_line = _finalized_line(env, env["part"], "5")
    reused_primary = receive_stock_lot(
        create_stock_lot(reused_primary_line, cells[2], Decimal("5"))
    )
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, reused_primary, Decimal("1"), unit_price=Decimal("100"))
    sale = complete_sale(sale, by=env["admin"])
    reused_ret = create_return(
        source=Sale.objects.get(pk=sale.pk), reason="Existing primary", by=env["admin"]
    )
    add_sale_line_return(
        reused_ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=cells[2], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(reused_ret, by=env["admin"])

    reused_legacy_line = _finalized_line(env, env["part"], "5")
    reused_legacy = _flip(
        create_stock_lot(reused_legacy_line, cells[3], Decimal("5"))
    )
    _age(reused_legacy, 3600)
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, reused_legacy, Decimal("1"), unit_price=Decimal("100"))
    sale = complete_sale(sale, by=env["admin"])
    reused_ret = create_return(
        source=Sale.objects.get(pk=sale.pk), reason="Existing legacy", by=env["admin"]
    )
    add_sale_line_return(
        reused_ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=cells[3], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(reused_ret, by=env["admin"])

    # A pending supplier lot near a same-part transfer is not transfer evidence.
    pending_line = _finalized_line(env, env["part"], "5")
    pending = create_stock_lot(pending_line, cells[1], Decimal("2"))

    classified = [
        lot
        for line in BatchLine.objects.filter(pk__in=StockLot.objects.values("batch_line_id"))
        for lot in line_provenance_detail(line).lots
    ]
    by_class = {}
    for lot in classified:
        by_class.setdefault(lot.provenance, set()).add(lot.lot_id)
    assert by_class[TRANSFER_DERIVED] == {
        target.pk, moved.pk, broken_target.pk,
    }
    assert by_class[RETURN_DERIVED] == {returned_lot.pk}
    assert next(
        row.provenance for row in classified if row.lot_id == reused_primary.pk
    ) == PRIMARY_RECEIPT
    assert next(
        row.provenance for row in classified if row.lot_id == reused_legacy.pk
    ) == LEGACY_PRIMARY
    assert next(row.provenance for row in classified if row.lot_id == pending.pk) == PENDING_RECEIPT
    assert next(
        item.provenance for item in classified if item.lot_id == rebound_target.pk
    ) == UNKNOWN
    assert next(
        item.provenance for item in classified if item.lot_id == tampered_target.pk
    ) == UNKNOWN

    assert {row["lot_id"] for row in _run("transfer_evidence")} == by_class[TRANSFER_DERIVED]
    assert {row["lot_id"] for row in _run("return_origin_evidence")} == by_class[RETURN_DERIVED]
    assert {row["lot_id"] for row in _run("reassigned_receipts")} == by_class[REASSIGNED]
    backfill = _run("old_backfill_receipts")
    assert [(row["lot_id"], row["lot_also_has_real_receipt"]) for row in backfill] == [
        (target.pk, False)
    ]
    assert _run("receipts_over_line") == []

    inventory = _run("lot_inventory")
    assert sum(row["lots"] for row in inventory) == StockLot.objects.count() == len(classified)
    no_movement = Counter()
    for row in inventory:
        no_movement[row["no_movement"]] += row["lots"]
    assert no_movement[True] == StockLot.objects.exclude(
        pk__in=StockMovement.objects.filter(stock_lot__isnull=False).values("stock_lot")
    ).count()
