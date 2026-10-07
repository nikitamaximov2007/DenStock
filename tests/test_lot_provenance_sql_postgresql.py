"""The read-only production SQL for lot provenance agrees with the classifier.

`docs/audits/lot-provenance-readonly.sql` is what an auditor runs against the
production database (the deployed code has no audit command yet). Each query
here runs in a READ ONLY transaction on PostgreSQL 16 over a ledger holding
every evidence case, and must name exactly the lots the classifier names.
"""
import re
from collections import Counter
from datetime import timedelta
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
from tests.lot_provenance_sql_parity import assert_final_provenance_parity
from tests.test_lot_provenance_adversarial import (  # noqa: F401
    _age,
    _corrupt_lot_for_adversarial_test,
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


def _queries(*, pre_origin_schema=False):
    text = SQL.read_text(encoding="utf-8")
    if pre_origin_schema:
        # Exercise the legacy evidence path as if 0016/0017 columns were absent.
        text = text.replace(
            "nullif(to_jsonb(l)->>'origin_transfer_id', '')::bigint", "NULL::bigint"
        ).replace(
            "nullif(to_jsonb(l)->>'origin_return_line_id', '')::bigint", "NULL::bigint"
        ).replace(
            "nullif(to_jsonb(l)->>'creation_origin', '')", "NULL::text"
        )
    parts = re.split(r"^-- name: (\w+)\n", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _run(name, *, pre_origin_schema=False):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(_queries(pre_origin_schema=pre_origin_schema)[name])
        columns = [c.name for c in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def test_the_file_only_reads():
    text = SQL.read_text(encoding="utf-8")
    assert set(_queries()) == {
        "lot_inventory", "supplier_receipt_evidence", "transfer_evidence",
        "reassigned_receipts",
        "old_backfill_receipts", "receipts_over_line", "return_origin_evidence",
        "final_provenance",
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

    # A detectable contradiction in historical return evidence must be
    # UNKNOWN in Python and absent from the operational positive-evidence SQL.
    damaged_line = _finalized_line(env, env["part"], "4")
    damaged_source = receive_stock_lot(
        create_stock_lot(damaged_line, cells[5], Decimal("4"))
    )
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, damaged_source, Decimal("1"), unit_price=Decimal("100"))
    sale = complete_sale(sale, by=env["admin"])
    damaged_return = create_return(
        source=Sale.objects.get(pk=sale.pk), reason="Damaged historical return", by=env["admin"]
    )
    damaged_return_line = add_sale_line_return(
        damaged_return, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=cells[4], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(damaged_return, by=env["admin"])
    damaged_target = damaged_return.lines.get().returned_lot
    _corrupt_lot_for_adversarial_test(
        damaged_target, origin_return_line=None, creation_origin=None
    )
    type(damaged_return_line).objects.filter(pk=damaged_return_line.pk).update(
        quantity=Decimal("0.5")
    )
    assert next(
        item.provenance for item in line_provenance_detail(damaged_line).lots
        if item.lot_id == damaged_target.pk
    ) == UNKNOWN

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

    # A transfer-origin lot later receives a customer return into that same
    # lot. The return is a later flow and must not change the lot's origin.
    transfer_return_line = _finalized_line(env, env["part"], "5")
    address_index = 1
    while any(
        StorageLocation.objects.filter(code=f"S99-D99-C{address_index + offset:02d}").exists()
        for offset in (0, 1)
    ):
        address_index += 2
    isolated_source = StorageLocation.objects.create(
        name="Transfer return source", code=f"S99-D99-C{address_index:02d}",
        storage_allowed=True, is_active=True,
    )
    isolated_target = StorageLocation.objects.create(
        name="Transfer return target", code=f"S99-D99-C{address_index + 1:02d}",
        storage_allowed=True, is_active=True,
    )
    transfer_source = receive_stock_lot(
        create_stock_lot(transfer_return_line, isolated_source, Decimal("5"))
    )
    _transfer(env, "5", isolated_source, isolated_target, "sql-transfer-later-return")
    transfer_target = StockLot.objects.get(
        batch_line=transfer_return_line, location=isolated_target
    )
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, transfer_target, Decimal("1"), unit_price=Decimal("100"))
    sale = complete_sale(sale, by=env["admin"])
    transfer_return = create_return(
        source=Sale.objects.get(pk=sale.pk), reason="Transfer later return", by=env["admin"]
    )
    add_sale_line_return(
        transfer_return, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=isolated_target, restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(transfer_return, by=env["admin"])
    transfer_target.refresh_from_db()
    assert transfer_target.origin_transfer_id is not None
    assert transfer_target.origin_return_line_id is None
    assert next(
        item.provenance for item in line_provenance_detail(transfer_return_line).lots
        if item.lot_id == transfer_source.pk
    ) == PRIMARY_RECEIPT

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
    assert {
        row["lot_id"]: row["provenance"] for row in _run("final_provenance")
    } == {lot.lot_id: lot.provenance for lot in classified}
    assert by_class[TRANSFER_DERIVED] == {
        target.pk, moved.pk, broken_target.pk, transfer_target.pk,
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
    assert {
        row["lot_id"] for row in _run("supplier_receipt_evidence")
    } == by_class[PRIMARY_RECEIPT] | by_class[REASSIGNED]
    for pre_origin_schema in (False, True):
        assert {
            row["lot_id"]
            for row in _run("return_origin_evidence", pre_origin_schema=pre_origin_schema)
        } == by_class[RETURN_DERIVED] == {returned_lot.pk}
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
    assert_final_provenance_parity()


@pytest.mark.parametrize("draft_delay_seconds", [0, 10, 600, 30 * 24 * 60 * 60])
def test_delayed_historical_return_python_sql_parity(  # noqa: F811
    units, env, draft_delay_seconds  # noqa: F811
):
    from django.utils import timezone

    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, source, Decimal("2"), unit_price=Decimal("100"))
    sale = complete_sale(sale, by=env["admin"])
    ret = create_return(
        source=Sale.objects.get(pk=sale.pk),
        reason="Отложенный возврат",
        by=env["admin"],
    )
    return_line = add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=env["cells"][1], restock_status=StockLot.Status.AVAILABLE,
    )
    if draft_delay_seconds:
        type(return_line).objects.filter(pk=return_line.pk).update(
            created_at=timezone.now() - timedelta(seconds=draft_delay_seconds)
        )
    complete_return(ret, by=env["admin"])
    returned = ret.lines.get().returned_lot
    # Model a pre-0017 historical lot while retaining the delayed draft evidence.
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(StockLot._meta.db_table)} "
            f"SET {connection.ops.quote_name('origin_return_line_id')} = NULL, "
            f"{connection.ops.quote_name('creation_origin')} = NULL WHERE id = %s",
            [returned.pk],
        )

    classified = next(
        row for row in line_provenance_detail(line).lots if row.lot_id == returned.pk
    )
    assert classified.provenance == RETURN_DERIVED
    for pre_origin_schema in (False, True):
        evidence = _run("return_origin_evidence", pre_origin_schema=pre_origin_schema)
        assert {row["lot_id"] for row in evidence} == {returned.pk}
    assert_final_provenance_parity()


def test_later_return_into_found_stock_keeps_found_origin(units, env):  # noqa: F811
    from apps.inventory.lot_provenance import FOUND_STOCK

    post_found_stock_group(
        entries=[{"source": "warehouse", "source_id": env["part"].pk,
                  "exact_number": "ADV-1", "quantity": 3}],
        location=env["cells"][3], token="sql-found-later-return",
    )
    found = StockLot.objects.get(part_type=env["part"], location=env["cells"][3])
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, found, Decimal("1"), unit_price=Decimal("100"))
    sale = complete_sale(sale, by=env["admin"])
    ret = create_return(source=Sale.objects.get(pk=sale.pk), reason="Later return", by=env["admin"])
    add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=env["cells"][3], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(ret, by=env["admin"])

    assert next(
        row.provenance for row in line_provenance_detail(found.batch_line).lots
        if row.lot_id == found.pk
    ) == FOUND_STOCK
    assert_final_provenance_parity()
