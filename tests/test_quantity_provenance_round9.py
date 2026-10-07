"""The source of a transfer opening must be the lot that actually moved."""

from decimal import Decimal

import pytest
from django.db import connection

from apps.inventory.lot_provenance import TRANSFER_DERIVED, UNKNOWN
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, move_stock_lot, receive_stock_lot
from tests.test_lot_provenance_adversarial import (  # noqa: F401
    _cls,
    _corrupt_lot_for_adversarial_test,
    _transfer,
    env,
)
from tests.test_lot_provenance_sql_postgresql import _run
from tests.test_piece_stock_boundary import _finalized_line, stock  # noqa: F401

# ruff: noqa: F811 - imported pytest fixtures are used as test arguments

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="SQL parity needs PostgreSQL"),
]


def _sql_class(lot):
    return next(row["provenance"] for row in _run("final_provenance") if row["lot_id"] == lot.pk)


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("replacement", ["target", "later_other_cell"])
def test_transfer_source_identity_cannot_be_replaced(env, historical, replacement):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "r9-source")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    movement = StockMovement.objects.get(
        document_type="stock_transfer", document_id=transfer.pk
    )
    if historical:
        _corrupt_lot_for_adversarial_test(
            target, origin_transfer=None, creation_origin=None
        )
        target.refresh_from_db()
    assert (_cls(line, target)[0], _sql_class(target)) == (
        TRANSFER_DERIVED, TRANSFER_DERIVED
    )

    if replacement == "target":
        other = target
    else:
        other = StockLot.objects.create(
            part_type=target.part_type, batch=target.batch, batch_line=line,
            location=env["cells"][3], quantity=Decimal("1"),
            initial_quantity=Decimal("1"), status=StockLot.Status.AVAILABLE,
            creation_origin=StockLot.CreationOrigin.SUPPLIER_PENDING,
        )
        assert other.created_at > movement.created_at
    StockMovement.objects.filter(pk=movement.pk).update(stock_lot=other)
    assert (_cls(line, target)[0], _sql_class(target)) == (UNKNOWN, UNKNOWN)


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("damage", [
    "wrong_cell", "wrong_part", "wrong_line", "wrong_batch", "unrelated_same_part",
    "unrelated_same_quantity", "later_source_cell", "rebound", "document_source_cell",
    "broken_location_history", "ambiguous_source", "source_quantity",
])
def test_transfer_source_adversarial_matrix(env, historical, damage):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "r9-matrix")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    movement = StockMovement.objects.get(
        document_type="stock_transfer", document_id=transfer.pk
    )
    if historical:
        _corrupt_lot_for_adversarial_test(
            target, origin_transfer=None, creation_origin=None
        )
        target.refresh_from_db()
    assert (_cls(line, target)[0], _sql_class(target)) == (
        TRANSFER_DERIVED, TRANSFER_DERIVED
    )

    if damage == "wrong_cell":
        StockLot.objects.filter(pk=source.pk).update(location=env["cells"][3])
    elif damage == "wrong_part":
        other = env["part"].__class__.objects.create(
            name="Другой источник", category=env["part"].category,
            unit=env["part"].unit, tracking_mode=env["part"].tracking_mode,
        )
        StockLot.objects.filter(pk=source.pk).update(part_type=other)
    elif damage in {"wrong_line", "rebound"}:
        other = _finalized_line(env, env["part"], "10")
        StockLot.objects.filter(pk=source.pk).update(batch_line=other)
    elif damage == "wrong_batch":
        other = _finalized_line(env, env["part"], "10")
        StockLot.objects.filter(pk=source.pk).update(batch=other.batch)
    elif damage in {"unrelated_same_part", "unrelated_same_quantity"}:
        quantity = Decimal("3" if damage == "unrelated_same_quantity" else "1")
        other = StockLot.objects.create(
            part_type=source.part_type, batch=source.batch, batch_line=line,
            location=env["cells"][3], quantity=quantity, initial_quantity=quantity,
            status=StockLot.Status.AVAILABLE,
            creation_origin=StockLot.CreationOrigin.SUPPLIER_PENDING,
        )
        StockLot.objects.filter(pk=other.pk).update(created_at=source.created_at)
        other.refresh_from_db()
        assert other.created_at < movement.created_at
        StockMovement.objects.filter(pk=movement.pk).update(stock_lot=other)
    elif damage == "later_source_cell":
        move_stock_lot(source, env["cells"][4])
        other = StockLot.objects.create(
            part_type=source.part_type, batch=source.batch, batch_line=line,
            location=env["cells"][0], quantity=Decimal("3"),
            initial_quantity=Decimal("3"), status=StockLot.Status.AVAILABLE,
            creation_origin=StockLot.CreationOrigin.SUPPLIER_PENDING,
        )
        assert other.created_at > movement.created_at
        StockMovement.objects.filter(pk=movement.pk).update(stock_lot=other)
    elif damage == "document_source_cell":
        transfer.from_location = env["cells"][3]
        transfer.save(update_fields=["from_location"])
    elif damage == "broken_location_history":
        move_stock_lot(source, env["cells"][4])
        StockMovement.objects.filter(
            stock_lot=source, movement_type=StockMovement.MovementType.MOVE_LOT,
            document_type="",
        ).update(from_location=env["cells"][3])
    elif damage == "ambiguous_source":
        other = StockLot.objects.create(
            part_type=source.part_type, batch=source.batch, batch_line=line,
            location=env["cells"][3], quantity=Decimal("3"),
            initial_quantity=Decimal("3"), status=StockLot.Status.AVAILABLE,
            creation_origin=StockLot.CreationOrigin.SUPPLIER_PENDING,
        )
        StockLot.objects.filter(pk=other.pk).update(created_at=source.created_at)
        StockMovement.objects.create(
            movement_type=StockMovement.MovementType.MOVE_LOT,
            part_type=source.part_type, stock_lot=other, batch=line.batch,
            batch_line=line, from_location=env["cells"][0],
            to_location=env["cells"][3], quantity=Decimal("3"),
        )
    elif damage == "source_quantity":
        StockLot.objects.filter(pk=source.pk).update(quantity=Decimal("9"))
    assert (_cls(line, target)[0], _sql_class(target)) == (UNKNOWN, UNKNOWN)


def test_later_whole_lot_moves_preserve_source_identity(env):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "r9-valid-moves")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    move_stock_lot(source, env["cells"][2])
    move_stock_lot(target, env["cells"][3])
    assert (_cls(line, target)[0], _sql_class(target)) == (
        TRANSFER_DERIVED, TRANSFER_DERIVED
    )
