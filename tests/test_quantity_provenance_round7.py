"""Round 7 regressions for transfer notes and creation-origin markers."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.urls import reverse

from apps.inventory.lot_provenance import (
    FOUND_STOCK,
    PENDING_RECEIPT,
    PRIMARY_RECEIPT,
    TRANSFER_DERIVED,
    UNKNOWN,
)
from apps.inventory.models import StockLot, StockMovement, StockTransfer
from apps.inventory.services import create_stock_lot, post_found_stock_group, receive_stock_lot
from apps.returns.models import StockReturn
from apps.returns.services import complete_return
from tests.test_lot_provenance_adversarial import (  # noqa: F401
    _cls,
    _corrupt_lot_for_adversarial_test,
    _transfer,
    env,
)
from tests.test_lot_provenance_sql_postgresql import _run
from tests.test_piece_stock_boundary import _finalized_line, stock  # noqa: F401
from tests.test_quantity_provenance_round6 import _return_created

# ruff: noqa: F811 - imported pytest fixtures are used as test arguments

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="SQL parity needs PostgreSQL"),
]


def _sql_class(lot):
    return next(row["provenance"] for row in _run("final_provenance") if row["lot_id"] == lot.pk)


@pytest.mark.parametrize("mode,malformation", [
    ("historical", "no_digits"),
    ("historical", "space"),
    ("explicit", "unicode_wrong_id"),
    ("historical", "unicode_same_id"),
    ("historical", "wrong_ascii_id"),
    ("explicit", "wrong_ascii_id"),
])
def test_admin_edited_transfer_note_fails_closed_in_python_and_sql(
    env, client, mode, malformation
):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1],
                         f"r7-admin-note-{mode}-{malformation}")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    assert _cls(line, target)[0] == _sql_class(target) == TRANSFER_DERIVED
    if mode == "historical":
        _corrupt_lot_for_adversarial_test(
            target, origin_transfer=None, creation_origin=None
        )
        assert _cls(line, target)[0] == _sql_class(target) == TRANSFER_DERIVED
    notes = {
        "no_digits": "Перемещение #без номера",
        "space": f"Перемещение # {transfer.pk} из ячейки",
        "unicode_wrong_id": "Перемещение #" + "".join(
            chr(ord("٠") + int(digit)) for digit in str(transfer.pk + 1)
        ) + " из ячейки",
        "unicode_same_id": "Перемещение #" + "".join(
            chr(ord("٠") + int(digit)) for digit in str(transfer.pk)
        ) + f" из {transfer.from_location_code}",
        "wrong_ascii_id": f"Перемещение #{transfer.pk + 1} из {transfer.from_location_code}",
    }
    client.force_login(env["admin"])
    response = client.post(
        reverse("admin:inventory_stocklot_change", args=[target.pk]),
        {"note": notes[malformation], "_save": "Сохранить"},
    )
    assert response.status_code == 302
    target.refresh_from_db()
    assert target.note == notes[malformation]
    assert _cls(line, target)[0] == _sql_class(target) == UNKNOWN


def test_historical_note_cannot_select_one_of_two_plausible_transfers(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "r7-first-transfer")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    _corrupt_lot_for_adversarial_test(target, origin_transfer=None, creation_origin=None)
    assert _cls(line, target)[0] == _sql_class(target) == TRANSFER_DERIVED
    second = _transfer(env, "3", env["cells"][0], env["cells"][1], "r7-second-transfer")
    StockTransfer.objects.filter(pk=second.pk).update(created_at=target.created_at)
    StockMovement.objects.filter(
        document_type="stock_transfer", document_id=second.pk
    ).update(created_at=target.created_at + timedelta(milliseconds=100))
    assert _cls(line, target)[0] == _sql_class(target) == UNKNOWN


@pytest.mark.parametrize("historical", [False, True])
def test_note_referring_to_another_existing_transfer_is_unknown(env, client, historical):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "r7-note-own")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    other = _transfer(env, "2", env["cells"][0], env["cells"][2], "r7-note-other")
    if historical:
        _corrupt_lot_for_adversarial_test(target, origin_transfer=None, creation_origin=None)
    client.force_login(env["admin"])
    response = client.post(
        reverse("admin:inventory_stocklot_change", args=[target.pk]),
        {"note": f"Перемещение #{other.pk} из {other.from_location_code}", "_save": "Сохранить"},
    )
    assert response.status_code == 302
    assert _cls(line, target)[0] == _sql_class(target) == UNKNOWN


@pytest.mark.parametrize("invalid", ["", " ", "supplier-received", "wrong"])
def test_invalid_creation_origin_rejected_by_orm_and_database(env, invalid):
    line = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(
        create_stock_lot(line, env["cells"][0], Decimal("4"))
    )
    with pytest.raises(ValidationError):
        StockLot.objects.filter(pk=lot.pk).update(creation_origin=invalid)
    lot.creation_origin = invalid
    with pytest.raises(ValidationError):
        lot.save()
    with pytest.raises(ValidationError):
        StockLot.objects.bulk_update([lot], ["creation_origin"])
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE inventory_stocklot SET creation_origin = %s WHERE id = %s",
                [invalid, lot.pk],
            )
    lot.refresh_from_db()
    assert lot.creation_origin == StockLot.CreationOrigin.SUPPLIER_RECEIVED
    assert _cls(line, lot)[0] == _sql_class(lot) == PRIMARY_RECEIPT


@pytest.mark.parametrize("marker,expected", [
    (None, PRIMARY_RECEIPT),
    ("supplier_received", PRIMARY_RECEIPT),
    ("", UNKNOWN), (" ", UNKNOWN), ("bogus", UNKNOWN),
    ("supplier_pending", UNKNOWN), ("found", UNKNOWN), ("recount", UNKNOWN),
    ("transfer", UNKNOWN), ("return", UNKNOWN),
])
def test_pre_constraint_marker_corruption_is_fail_closed_in_both_readers(env, marker, expected):
    line = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("4")))
    # A temporary table shadows the stock-lot table for this read-only audit.
    # It models a pre-0019 corrupted row without weakening the real DB constraint.
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE inventory_stocklot AS TABLE public.inventory_stocklot"
        )
        cursor.execute(
            "UPDATE inventory_stocklot SET creation_origin = %s WHERE id = %s",
            [marker, lot.pk],
        )
    try:
        assert _cls(line, lot)[0] == _sql_class(lot) == expected
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE inventory_stocklot")


@pytest.mark.parametrize("source,marker,expected", [
    ("pending", None, PENDING_RECEIPT),
    ("pending", "", UNKNOWN),
    ("found", "found", FOUND_STOCK),
    ("found", "", UNKNOWN),
    ("found", "bogus", UNKNOWN),
])
def test_corrupt_marker_cannot_keep_other_positive_provenance(env, source, marker, expected):
    if source == "pending":
        line = _finalized_line(env, env["part"], "10")
        lot = create_stock_lot(line, env["cells"][0], Decimal("4"))
    else:
        post_found_stock_group(
            entries=[{"source": "warehouse", "source_id": env["part"].pk,
                      "exact_number": "ADV-1", "quantity": 2}],
            location=env["cells"][4], token=f"r7-marker-{marker}",
        )
        lot = StockLot.objects.get(location=env["cells"][4], creation_origin="found")
        line = lot.batch_line
    with connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE inventory_stocklot AS TABLE public.inventory_stocklot")
        cursor.execute(
            "UPDATE inventory_stocklot SET creation_origin = %s WHERE id = %s",
            [marker, lot.pk],
        )
    try:
        assert _cls(line, lot)[0] == _sql_class(lot) == expected
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE inventory_stocklot")


@pytest.mark.xfail(strict=True, reason="pre-existing completed-return status integrity gap")
def test_completed_return_admin_status_reset_cannot_post_stock_twice(env, client):
    _, target, stock_return = _return_created(env)
    return_line = stock_return.lines.get()
    prior_count = StockMovement.objects.filter(
        document_type="stock_return", document_id=stock_return.pk,
        movement_type=StockMovement.MovementType.RETURN_LOT,
    ).count()
    prior_quantity = target.quantity
    client.force_login(env["admin"])
    response = client.post(
        reverse("admin:returns_stockreturn_change", args=[stock_return.pk]),
        {
            "status": StockReturn.Status.DRAFT,
            "source_type": stock_return.source_type,
            "source_id": stock_return.source_id,
            "reason": stock_return.reason,
            "comment": stock_return.comment,
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "1",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-id": str(return_line.pk),
            "lines-0-stock_return": str(stock_return.pk),
        },
    )
    assert response.status_code == 302
    stock_return.refresh_from_db()
    assert stock_return.status == StockReturn.Status.DRAFT
    complete_return(stock_return, by=env["admin"])
    target.refresh_from_db()
    assert StockMovement.objects.filter(
        document_type="stock_return", document_id=stock_return.pk,
        movement_type=StockMovement.MovementType.RETURN_LOT,
    ).count() == prior_count
    assert target.quantity == prior_quantity
