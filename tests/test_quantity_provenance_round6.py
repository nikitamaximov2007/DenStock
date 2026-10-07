"""Round 6 regressions for forged or erased lot-origin evidence."""
# ruff: noqa: F811 - imported pytest fixture names are used as test arguments

from decimal import Decimal

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.urls import reverse

from apps.inventory.lot_provenance import TRANSFER_DERIVED, UNKNOWN
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.returns.models import StockReturn, StockReturnLine
from apps.returns.services import (
    add_sale_line_return,
    cancel_return,
    complete_return,
    create_return,
)
from apps.sales.models import Sale
from tests.public_catalog_support import public_catalog  # noqa: F401
from tests.test_lot_provenance_adversarial import (  # noqa: F401
    _age,
    _cls,
    _corrupt_lot_for_adversarial_test,
    _erase_return_document_for_adversarial_test,
    _flip,
    _sell,
    _transfer,
    env,
)
from tests.test_piece_stock_boundary import _finalized_line, stock  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def final_sql_class_parity():
    """Check final classes after each Round 6 blocker and corruption fixture."""
    yield
    if connection.vendor == "postgresql":
        from tests.lot_provenance_sql_parity import assert_final_provenance_parity

        assert_final_provenance_parity()


def _return_created(env, *, location=None):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = _sell(env, source, "2")
    stock_return = create_return(
        source=Sale.objects.get(pk=sale.pk), reason="Audit", by=env["admin"]
    )
    add_sale_line_return(
        stock_return, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=location or env["cells"][1], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(stock_return, by=env["admin"])
    returned = stock_return.lines.get().returned_lot
    assert returned.creation_origin == StockLot.CreationOrigin.RETURN
    assert returned.origin_return_line_id == stock_return.lines.get().pk
    return line, returned, stock_return


def test_explicit_return_cannot_become_supplier_by_retyping_movement(env):
    line, target, _ = _return_created(env)
    StockMovement.objects.filter(stock_lot=target, movement_type="return_lot").update(
        movement_type="receive_lot"
    )
    assert _cls(line, target)[0] == UNKNOWN


def test_erased_historical_return_cannot_become_legacy_supplier(env):
    line, target, stock_return = _return_created(env)
    _corrupt_lot_for_adversarial_test(
        target, origin_return_line=None, creation_origin=None
    )
    StockMovement.objects.filter(stock_lot=target, movement_type="return_lot").delete()
    _erase_return_document_for_adversarial_test(stock_return)
    assert _cls(line, target)[0] == UNKNOWN


def test_pre_marker_supplier_lot_without_receipt_evidence_is_unknown(env):
    line = _finalized_line(env, env["part"], "10")
    lot = _flip(create_stock_lot(line, env["cells"][0], Decimal("2")))
    _corrupt_lot_for_adversarial_test(lot, creation_origin=None)
    assert not StockMovement.objects.filter(stock_lot=lot).exists()
    assert _cls(line, lot)[0] == UNKNOWN


def test_later_return_link_cannot_rewrite_existing_supplier_origin(env):
    line = _finalized_line(env, env["part"], "10")
    legacy = _flip(create_stock_lot(line, env["cells"][0], Decimal("2")))
    _age(legacy, 3600)
    sale = _sell(env, legacy, "2")
    stock_return = create_return(source=sale, reason="Audit", by=env["admin"])
    add_sale_line_return(
        stock_return, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=env["cells"][0], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(stock_return, by=env["admin"])
    _corrupt_lot_for_adversarial_test(
        legacy, origin_return_line=stock_return.lines.get().pk
    )
    assert _cls(line, legacy)[0] == UNKNOWN


@pytest.mark.parametrize("movement_type", ["return_lot", "adjust_in", "receive_lot"])
def test_extra_contradictory_return_row_invalidates_explicit_origin(env, movement_type):
    line, target, _ = _return_created(env)
    original = StockMovement.objects.get(stock_lot=target, movement_type="return_lot")
    StockMovement.objects.create(
        movement_type=movement_type, part_type=original.part_type,
        stock_lot=target, batch=original.batch, batch_line=original.batch_line,
        from_location=original.from_location, to_location=original.to_location,
        quantity=Decimal("1"), unit_cost_rub=original.unit_cost_rub,
        document_type=original.document_type, document_id=original.document_id,
    )
    assert _cls(line, target)[0] == UNKNOWN


def test_later_transfer_link_cannot_rewrite_existing_supplier_origin(env):
    line = _finalized_line(env, env["part"], "10")
    legacy = _flip(create_stock_lot(line, env["cells"][0], Decimal("2")))
    _age(legacy, 3600)
    receive_stock_lot(create_stock_lot(line, env["cells"][1], Decimal("4")))
    transfer = _transfer(env, "2", env["cells"][1], env["cells"][0], "r6-later-transfer")
    _corrupt_lot_for_adversarial_test(legacy, origin_transfer=transfer.pk)
    assert _cls(line, legacy)[0] == UNKNOWN


def test_retyped_found_stock_cannot_become_supplier_receipt(env):
    from apps.inventory.services import post_found_stock_group

    lot = post_found_stock_group(
        entries=[{"source": "warehouse", "source_id": env["part"].pk,
                  "exact_number": "ADV-1", "quantity": 2}],
        location=env["cells"][0], token="r6-forged-found",
    )[0]["lot"]
    StockMovement.objects.filter(stock_lot=lot, movement_type="adjust_in").update(
        movement_type="receive_lot", document_type="", document_id=None,
        quantity=Decimal("2"),
    )
    assert _cls(lot.batch_line, lot)[0] == UNKNOWN


def test_new_supplier_and_transfer_creation_markers_are_persisted(env):
    line = _finalized_line(env, env["part"], "10")
    source = create_stock_lot(line, env["cells"][0], Decimal("4"))
    assert source.creation_origin == StockLot.CreationOrigin.SUPPLIER_PENDING
    receive_stock_lot(source)
    source.refresh_from_db()
    assert source.creation_origin == StockLot.CreationOrigin.SUPPLIER_RECEIVED
    transfer = _transfer(env, "2", env["cells"][0], env["cells"][1], "r6-new-transfer")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    assert target.creation_origin == StockLot.CreationOrigin.TRANSFER
    assert target.origin_transfer_id == transfer.pk
    target.creation_origin = StockLot.CreationOrigin.SUPPLIER_RECEIVED
    with pytest.raises(ValidationError):
        target.save(update_fields=["creation_origin"])


@pytest.mark.parametrize("damage", ["document", "line", "quantity", "duplicate"])
def test_supplier_receipt_requires_exact_ledger_context(env, damage):
    line = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("4")))
    receipt = StockMovement.objects.get(stock_lot=lot, movement_type="receive_lot")
    if damage == "document":
        StockMovement.objects.filter(pk=receipt.pk).update(
            document_type="stock_return", document_id=123
        )
    elif damage == "line":
        other = _finalized_line(env, env["part"], "4")
        StockMovement.objects.filter(pk=receipt.pk).update(batch_line=other)
    elif damage == "quantity":
        StockMovement.objects.filter(pk=receipt.pk).update(quantity=Decimal("3"))
    else:
        StockMovement.objects.create(
            movement_type="receive_lot", part_type=lot.part_type, stock_lot=lot,
            batch=lot.batch, batch_line=lot.batch_line, to_location=lot.location,
            quantity=Decimal("1"), unit_cost_rub=receipt.unit_cost_rub,
        )
    assert _cls(line, lot)[0] == UNKNOWN


@pytest.mark.parametrize("historical", [False, True])
def test_completed_return_admin_delete_is_forbidden(env, client, historical):
    line, target, stock_return = _return_created(env)
    if historical:
        _corrupt_lot_for_adversarial_test(
            target, origin_return_line=None, creation_origin=None
        )
    movement = StockMovement.objects.get(stock_lot=target, movement_type="return_lot")
    client.force_login(env["admin"])
    response = client.post(
        reverse("admin:returns_stockreturn_delete", args=[stock_return.pk]),
        {"post": "yes"},
    )
    assert response.status_code == 403
    assert StockReturn.objects.filter(pk=stock_return.pk).exists()
    assert stock_return.lines.exists()
    assert StockMovement.objects.filter(pk=movement.pk).exists()


def test_historical_return_document_and_line_reject_direct_orm_deletion(env):
    _, target, stock_return = _return_created(env)
    _corrupt_lot_for_adversarial_test(
        target, origin_return_line=None, creation_origin=None
    )
    line = stock_return.lines.get()
    with pytest.raises(ProtectedError):
        stock_return.delete()
    with pytest.raises(ProtectedError):
        StockReturn.objects.filter(pk=stock_return.pk).delete()
    with pytest.raises(ProtectedError):
        line.delete()
    with pytest.raises(ProtectedError):
        StockReturnLine.objects.filter(pk=line.pk).delete()
    assert StockReturn.objects.filter(pk=stock_return.pk).exists()
    assert StockReturnLine.objects.filter(pk=line.pk).exists()
    assert StockMovement.objects.filter(
        document_type="stock_return", document_id=stock_return.pk
    ).exists()


def test_canceled_return_admin_delete_is_forbidden(env, client):
    _, _, stock_return = _return_created(env)
    cancel_return(stock_return, by=env["admin"], reason="Audit")
    client.force_login(env["admin"])
    response = client.post(
        reverse("admin:returns_stockreturn_delete", args=[stock_return.pk]),
        {"post": "yes"},
    )
    assert response.status_code == 403
    assert StockReturn.objects.filter(pk=stock_return.pk).exists()


def test_draft_and_bulk_return_admin_delete_are_forbidden(env, client):
    draft = StockReturn.objects.create(
        source_type=StockReturn.SourceType.SALE, source_id=1, created_by=env["admin"]
    )
    client.force_login(env["admin"])
    direct = client.post(
        reverse("admin:returns_stockreturn_delete", args=[draft.pk]), {"post": "yes"}
    )
    assert direct.status_code == 403
    bulk = client.post(
        reverse("admin:returns_stockreturn_changelist"),
        {"action": "delete_selected", "_selected_action": [draft.pk], "index": "0"},
    )
    assert bulk.status_code == 200
    assert StockReturn.objects.filter(pk=draft.pk).exists()
    assert not admin.site.is_registered(StockReturnLine)


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
def test_transfer_sql_rejects_extra_document_movement(env):
    from tests.test_lot_provenance_sql_postgresql import _run

    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "r6-sql-extra")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    original = StockMovement.objects.get(document_type="stock_transfer", document_id=transfer.pk)
    StockMovement.objects.create(
        movement_type="adjust_in", part_type=original.part_type,
        stock_lot=original.stock_lot, batch=original.batch, batch_line=original.batch_line,
        from_location=original.from_location, to_location=original.to_location,
        quantity=Decimal("1"), unit_cost_rub=original.unit_cost_rub,
        document_type="stock_transfer", document_id=transfer.pk,
    )
    assert _cls(line, target)[0] == UNKNOWN
    assert target.pk not in {row["lot_id"] for row in _run("transfer_evidence")}


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
def test_transfer_sql_ignores_changed_display_note_with_explicit_origin(env):
    from tests.test_lot_provenance_sql_postgresql import _run

    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "r6-sql-note")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    _corrupt_lot_for_adversarial_test(target, note="Перемещение #999999 из другого места")
    assert _cls(line, target)[0] == TRANSFER_DERIVED
    assert target.pk in {row["lot_id"] for row in _run("transfer_evidence")}


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
def test_transfer_sql_rejects_rebound_source_history(env):
    from tests.test_lot_provenance_sql_postgresql import _run

    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "r6-sql-source-rebound")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    other = _finalized_line(env, env["part"], "10")
    StockLot.objects.filter(pk=source.pk).update(batch_line=other)
    assert _cls(line, target)[0] == UNKNOWN
    assert target.pk not in {row["lot_id"] for row in _run("transfer_evidence")}


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
@pytest.mark.parametrize(
    "damage", ["type", "quantity", "cell", "line", "duplicate", "missing", "receipt"]
)
def test_transfer_damage_has_python_sql_parity(env, damage):
    from tests.test_lot_provenance_sql_postgresql import _run

    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], f"r6-{damage}")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    move = StockMovement.objects.get(document_type="stock_transfer", document_id=transfer.pk)
    if damage == "type":
        StockMovement.objects.filter(pk=move.pk).update(movement_type="adjust_in")
    elif damage == "quantity":
        StockMovement.objects.filter(pk=move.pk).update(quantity=Decimal("2"))
    elif damage == "cell":
        StockMovement.objects.filter(pk=move.pk).update(to_location=env["cells"][2])
    elif damage == "line":
        other_line = _finalized_line(env, env["part"], "3")
        StockMovement.objects.filter(pk=move.pk).update(batch_line=other_line)
    elif damage == "duplicate":
        StockMovement.objects.create(
            movement_type=move.movement_type, part_type=move.part_type,
            stock_lot=move.stock_lot, batch=move.batch, batch_line=move.batch_line,
            from_location=move.from_location, to_location=move.to_location,
            quantity=move.quantity, unit_cost_rub=move.unit_cost_rub,
            document_type=move.document_type, document_id=move.document_id,
        )
    elif damage == "missing":
        StockMovement.objects.filter(pk=move.pk).delete()
    else:
        StockMovement.objects.create(
            movement_type="receive_lot", part_type=target.part_type,
            stock_lot=target, batch=target.batch, batch_line=target.batch_line,
            to_location=target.location, quantity=target.initial_quantity,
            unit_cost_rub=target.landed_unit_cost_rub,
        )
    assert _cls(line, target)[0] == UNKNOWN
    assert target.pk not in {row["lot_id"] for row in _run("transfer_evidence")}


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
def test_return_sql_rejects_contradictory_document_movement(env):
    from tests.test_lot_provenance_sql_postgresql import _run

    line, target, stock_return = _return_created(env)
    original = StockMovement.objects.get(stock_lot=target, movement_type="return_lot")
    StockMovement.objects.create(
        movement_type="adjust_in", part_type=original.part_type,
        stock_lot=original.stock_lot, batch=original.batch, batch_line=original.batch_line,
        to_location=original.to_location, quantity=Decimal("1"),
        unit_cost_rub=original.unit_cost_rub,
        document_type="stock_return", document_id=stock_return.pk,
    )
    assert _cls(line, target)[0] == UNKNOWN
    assert target.pk not in {row["lot_id"] for row in _run("return_origin_evidence")}


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
@pytest.mark.parametrize(
    "damage", [
        "type", "quantity", "cell", "line", "batch", "part", "duplicate",
        "missing", "returned_lot", "draft",
    ]
)
def test_return_damage_has_python_sql_parity(env, damage):
    from tests.test_lot_provenance_sql_postgresql import _run

    line, target, stock_return = _return_created(env)
    movement = StockMovement.objects.get(stock_lot=target, movement_type="return_lot")
    return_line = stock_return.lines.get()
    if damage == "type":
        StockMovement.objects.filter(pk=movement.pk).update(movement_type="adjust_in")
    elif damage == "quantity":
        StockMovement.objects.filter(pk=movement.pk).update(quantity=Decimal("1"))
    elif damage == "cell":
        StockMovement.objects.filter(pk=movement.pk).update(to_location=env["cells"][2])
    elif damage == "line":
        other_line = _finalized_line(env, env["part"], "3")
        StockMovement.objects.filter(pk=movement.pk).update(batch_line=other_line)
    elif damage == "batch":
        other_line = _finalized_line(env, env["part"], "3")
        StockMovement.objects.filter(pk=movement.pk).update(batch=other_line.batch)
    elif damage == "part":
        other = env["part"].__class__.objects.create(
            name="Other origin part", category=env["part"].category,
            unit=env["part"].unit, tracking_mode=env["part"].tracking_mode,
        )
        StockMovement.objects.filter(pk=movement.pk).update(part_type=other)
    elif damage == "duplicate":
        StockMovement.objects.create(
            movement_type=movement.movement_type, part_type=movement.part_type,
            stock_lot=movement.stock_lot, batch=movement.batch,
            batch_line=movement.batch_line, to_location=movement.to_location,
            quantity=movement.quantity, unit_cost_rub=movement.unit_cost_rub,
            document_type=movement.document_type, document_id=movement.document_id,
        )
    elif damage == "missing":
        StockMovement.objects.filter(pk=movement.pk).delete()
    elif damage == "returned_lot":
        StockReturnLine.objects.filter(pk=return_line.pk).update(returned_lot=None)
    else:
        StockReturn.objects.filter(pk=stock_return.pk).update(status="draft", completed_at=None)
    assert _cls(line, target)[0] == UNKNOWN
    assert target.pk not in {row["lot_id"] for row in _run("return_origin_evidence")}


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
def test_supplier_sql_rejects_forged_return_receipt(env):
    from tests.test_lot_provenance_sql_postgresql import _run

    line, target, _ = _return_created(env)
    StockMovement.objects.filter(stock_lot=target, movement_type="return_lot").update(
        movement_type="receive_lot"
    )
    assert _cls(line, target)[0] == UNKNOWN
    assert target.pk not in {row["lot_id"] for row in _run("supplier_receipt_evidence")}


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
def test_return_document_wrong_lot_movement_rejects_python_and_sql(env):
    from tests.test_lot_provenance_sql_postgresql import _run

    line, target, stock_return = _return_created(env)
    unrelated = receive_stock_lot(
        create_stock_lot(_finalized_line(env, env["part"], "3"), env["cells"][2], Decimal("3"))
    )
    original = StockMovement.objects.get(stock_lot=target, movement_type="return_lot")
    StockMovement.objects.create(
        movement_type="return_lot", part_type=original.part_type,
        stock_lot=unrelated, batch=unrelated.batch, batch_line=unrelated.batch_line,
        to_location=unrelated.location, quantity=Decimal("1"),
        unit_cost_rub=original.unit_cost_rub,
        document_type="stock_return", document_id=stock_return.pk,
    )
    assert _cls(line, target)[0] == UNKNOWN
    assert target.pk not in {row["lot_id"] for row in _run("return_origin_evidence")}


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="production SQL needs PostgreSQL")
@pytest.mark.parametrize("damage", ["document", "line", "quantity", "duplicate"])
def test_supplier_sql_rejects_invalid_receipt_context(env, damage):
    from tests.test_lot_provenance_sql_postgresql import _run

    line = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("4")))
    receipt = StockMovement.objects.get(stock_lot=lot, movement_type="receive_lot")
    if damage == "document":
        StockMovement.objects.filter(pk=receipt.pk).update(
            document_type="stock_return", document_id=123
        )
    elif damage == "line":
        other = _finalized_line(env, env["part"], "4")
        StockMovement.objects.filter(pk=receipt.pk).update(batch_line=other)
    elif damage == "quantity":
        StockMovement.objects.filter(pk=receipt.pk).update(quantity=Decimal("3"))
    else:
        StockMovement.objects.create(
            movement_type="receive_lot", part_type=lot.part_type, stock_lot=lot,
            batch=lot.batch, batch_line=lot.batch_line, to_location=lot.location,
            quantity=Decimal("1"), unit_cost_rub=receipt.unit_cost_rub,
        )
    assert _cls(line, lot)[0] == UNKNOWN
    assert lot.pk not in {row["lot_id"] for row in _run("supplier_receipt_evidence")}
