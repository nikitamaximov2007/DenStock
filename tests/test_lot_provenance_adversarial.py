"""Adversarial review of lot provenance and the lifetime receipt cap.

Each test tries to make the classifier wrong: a non-primary lot passing as
a receipt, a receipt hidden or counted twice, capacity reopened by stock
moving around. A deterministic property test replays random operation mixes
against an oracle that only knows what was really received.
"""
import random
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.inventory.lot_provenance import (
    FOUND_STOCK,
    LEGACY_PRIMARY,
    PENDING_RECEIPT,
    PRIMARY_RECEIPT,
    RETURN_DERIVED,
    TRANSFER_DERIVED,
    UNKNOWN,
    line_provenance,
)
from apps.inventory.models import StockLot, StockMovement, StockTransfer
from apps.inventory.services import (
    InventoryError,
    add_found_stock,
    adjust_stock_lot_quantity,
    create_stock_lot,
    move_stock_lot,
    perform_stock_transfer,
    post_found_stock_group,
    receive_stock_lot,
    remaining_qty,
)
from apps.procurement.models import Batch, BatchLine
from apps.returns.models import StockReturn, StockReturnLine
from apps.returns.services import (
    add_sale_line_return,
    cancel_return,
    complete_return,
    create_return,
)
from apps.sales.models import Sale
from apps.sales.services import (
    add_stock_lot_to_sale,
    cancel_sale,
    complete_sale,
    create_sale,
)
from apps.warehouse.models import StorageLocation
from apps.writeoffs.models import WriteOffDocument
from apps.writeoffs.services import (
    add_stock_lot_to_write_off,
    cancel_write_off,
    complete_write_off,
    create_write_off,
)
from tests.customs_support import remember_customs
from tests.test_piece_stock_boundary import _finalized_line, stock  # noqa: F401

pytestmark = pytest.mark.django_db
RECEIPT = StockMovement.MovementType.RECEIVE_LOT


@pytest.fixture(autouse=True)
def final_sql_class_parity():
    """Every adversarial PostgreSQL fixture must agree on the final class."""
    yield
    if connection.vendor == "postgresql":
        from tests.lot_provenance_sql_parity import assert_final_provenance_parity

        assert_final_provenance_parity()


@pytest.fixture
def env(stock, public_catalog):  # noqa: F811
    cells = [
        StorageLocation.objects.create(
            name=f"Adv {n}", code=f"S09-D05-C0{n}", storage_allowed=True, is_active=True
        )
        for n in range(1, 7)
    ]
    part = public_catalog.part("Ремень", article="ADV-1", price="100")
    remember_customs(part)
    return {**stock, "cells": cells, "part": part}


def _cls(line, lot):
    for row in line_provenance(line):
        if row.lot_id == lot.pk:
            return row.provenance, row.intake
    raise AssertionError("lot not on line")


def _flip(lot):
    """The pre-108b5ad status button: AVAILABLE without receive_stock_lot."""
    StockLot.objects.filter(pk=lot.pk).update(status=StockLot.Status.AVAILABLE)
    lot.refresh_from_db()
    return lot


def _transfer(env, quantity, source, target, token):
    return perform_stock_transfer(
        part=env["part"], from_location=source, to_location=target, quantity=quantity,
        stock_state=StockLot.Status.AVAILABLE, token=token,
    )[0]


def _sell(env, lot, quantity):
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, lot, Decimal(quantity), unit_price=Decimal("100"))
    return complete_sale(sale, by=env["admin"])


def _lot_at(line, cell):
    return StockLot.objects.get(batch_line=line, location=cell)


def _corrupt_lot_for_adversarial_test(lot, **fields):
    """Bypass the app guard only to model pre-existing/direct-DB corruption."""
    columns = {
        "origin_transfer": "origin_transfer_id",
        "origin_return_line": "origin_return_line_id",
        "creation_origin": "creation_origin",
        "note": "note",
    }
    if not fields or set(fields) - columns.keys():
        raise AssertionError("Unexpected test-only corruption field")
    assignments = ", ".join(
        f"{connection.ops.quote_name(columns[name])} = %s" for name in fields
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(StockLot._meta.db_table)} "
            f"SET {assignments} WHERE id = %s",
            [*fields.values(), lot.pk],
        )


def _erase_return_document_for_adversarial_test(stock_return):
    """Model deletion is guarded; only this direct SQL models erased old history."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {connection.ops.quote_name(StockReturnLine._meta.db_table)} "
            "WHERE stock_return_id = %s", [stock_return.pk],
        )
        cursor.execute(
            f"DELETE FROM {connection.ops.quote_name(StockReturn._meta.db_table)} "
            "WHERE id = %s", [stock_return.pk],
        )


# --- B. LEGACY_PRIMARY: true positives through every kind of later history ------------


def _legacy_line(env, quantity="6"):
    line = _finalized_line(env, env["part"], "10")
    return line, _flip(create_stock_lot(line, env["cells"][0], Decimal(quantity)))


def test_legacy_survives_sale_return_cancellation_adjust_writeoff_and_moves(env):
    line, lot = _legacy_line(env)
    _age(lot, 3600)
    sale = _sell(env, lot, "2")
    stock_return = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        stock_return, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=env["cells"][0], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(stock_return, by=env["admin"])  # back into the legacy lot
    cancel_sale(_sell(env, lot, "1"), reason="Ошибка", author="Денис", by=env["admin"])
    adjust_stock_lot_quantity(lot, Decimal("1"), comment="Пересчёт +")
    adjust_stock_lot_quantity(lot, Decimal("-1"), comment="Пересчёт -")
    adjust_stock_lot_quantity(
        lot, Decimal("1"), comment="Пересчёт", document_type="section_recount"
    )
    add_found_stock(env["part"], env["cells"][0], Decimal("1"))
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=env["admin"])
    add_stock_lot_to_write_off(doc, lot, Decimal("2"))
    complete_write_off(doc, by=env["admin"])
    cancel_write_off(doc, by=env["admin"])
    _transfer(env, "1", env["cells"][0], env["cells"][1], "adv-l1")
    move_stock_lot(lot, env["cells"][2])  # whole move: the lot now lives in cell 2
    _transfer(env, "1", env["cells"][1], env["cells"][2], "adv-l2")  # merge into its new cell

    assert _cls(line, lot) == (LEGACY_PRIMARY, Decimal("6"))
    assert remaining_qty(line) == Decimal("4")


def test_later_return_into_legacy_lot_does_not_reopen_supplier_receipt_capacity(env):
    line = _finalized_line(env, env["part"], "16")
    legacy = _flip(create_stock_lot(line, env["cells"][0], Decimal("6")))
    _age(legacy, 3600)
    receive_stock_lot(create_stock_lot(line, env["cells"][1], Decimal("10")))
    assert remaining_qty(line) == Decimal("0")

    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, legacy, Decimal("6"), unit_price=Decimal("100"))
    complete_sale(sale, by=env["admin"])
    ret = create_return(source=Sale.objects.get(pk=sale.pk), reason="Возврат", by=env["admin"])
    add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("6"),
        to_location=env["cells"][0], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(ret, by=env["admin"])

    assert _cls(line, legacy) == (LEGACY_PRIMARY, Decimal("6"))
    legacy.refresh_from_db()
    assert legacy.origin_return_line_id is None
    assert remaining_qty(line) == Decimal("0")
    with pytest.raises(InventoryError):
        receive_stock_lot(create_stock_lot(line, env["cells"][2], Decimal("6")))


def test_later_return_does_not_change_primary_or_transfer_origin(env):
    primary_line = _finalized_line(env, env["part"], "10")
    primary = receive_stock_lot(
        create_stock_lot(primary_line, env["cells"][0], Decimal("10"))
    )
    sale = _sell(env, primary, "2")
    ret = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=env["cells"][0], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(ret, by=env["admin"])
    assert _cls(primary_line, primary) == (PRIMARY_RECEIPT, Decimal("10"))
    primary.refresh_from_db()
    assert primary.origin_return_line_id is None

    transfer_line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(transfer_line, env["cells"][2], Decimal("10")))
    _transfer(env, "4", env["cells"][2], env["cells"][3], "round4-return-transfer")
    target = _lot_at(transfer_line, env["cells"][3])
    sale = _sell(env, target, "1")
    ret = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=env["cells"][3], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(ret, by=env["admin"])
    target.refresh_from_db()
    assert target.origin_return_line_id is None
    assert _cls(transfer_line, target) == (TRANSFER_DERIVED, Decimal("0"))


def test_return_created_origin_survives_later_adjustment(env):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = _sell(env, source, "2")
    ret = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=env["cells"][1], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(ret, by=env["admin"])
    returned = ret.lines.get().returned_lot
    adjust_stock_lot_quantity(returned, Decimal("1"), comment="Сверка +")
    _transfer(env, "1", env["cells"][1], env["cells"][2], "round4-return-onward")

    assert returned.origin_return_line_id == ret.lines.get().pk
    assert _cls(line, returned) == (RETURN_DERIVED, Decimal("0"))
    returned.origin_return_line_id = None
    with pytest.raises(ValidationError):
        returned.save(update_fields=["origin_return_line"])


def test_historical_return_origin_requires_linked_first_exact_movement(env):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = _sell(env, source, "2")
    ret = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=env["cells"][1], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(ret, by=env["admin"])
    returned = ret.lines.get().returned_lot
    _corrupt_lot_for_adversarial_test(
        returned, origin_return_line=None, creation_origin=None
    )

    assert _cls(line, returned) == (RETURN_DERIVED, Decimal("0"))


@pytest.mark.parametrize("draft_delay_seconds", [0, 10, 600, 30 * 24 * 60 * 60])
def test_historical_return_origin_uses_completion_event_not_draft_line_time(
    env, draft_delay_seconds
):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = _sell(env, source, "2")
    ret = create_return(source=sale, reason="Отложенный возврат", by=env["admin"])
    return_line = add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=env["cells"][1], restock_status=StockLot.Status.AVAILABLE,
    )
    if draft_delay_seconds:
        StockReturnLine.objects.filter(pk=return_line.pk).update(
            created_at=timezone.now() - timedelta(seconds=draft_delay_seconds)
        )

    complete_return(ret, by=env["admin"])
    returned = StockReturnLine.objects.get(pk=return_line.pk).returned_lot
    _corrupt_lot_for_adversarial_test(
        returned, origin_return_line=None, creation_origin=None
    )

    assert _cls(line, returned) == (RETURN_DERIVED, Decimal("0"))


def test_cancelling_return_does_not_rewrite_return_created_lot_origin(env):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = _sell(env, source, "2")
    ret = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        ret, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=env["cells"][1], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(ret, by=env["admin"])
    returned = ret.lines.get().returned_lot

    cancel_return(ret, by=env["admin"], reason="Ошибка оформления")

    returned.refresh_from_db()
    assert returned.quantity == Decimal("0")
    assert _cls(line, returned) == (RETURN_DERIVED, Decimal("0"))


def test_sale_cancellation_restores_sold_out_legacy_lot_origin_and_capacity(env):
    line, lot = _legacy_line(env)
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, lot, Decimal("6"), unit_price=Decimal("100"))
    sale = complete_sale(sale, by=env["admin"])

    cancel_sale(sale, reason="Проверка", author="Денис", by=env["admin"])

    lot.refresh_from_db()
    assert lot.quantity == Decimal("6")
    assert _cls(line, lot) == (LEGACY_PRIMARY, Decimal("6"))
    assert remaining_qty(line) == Decimal("4")


def test_completed_transfer_record_cannot_be_deleted_without_origin_fk(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "1", env["cells"][0], env["cells"][1], "round4-delete")
    target = _lot_at(line, env["cells"][1])
    _corrupt_lot_for_adversarial_test(target, origin_transfer=None, creation_origin=None)
    with pytest.raises(ProtectedError):
        transfer.delete()
    with pytest.raises(ProtectedError):
        StockTransfer.objects.filter(pk=transfer.pk).delete()
    assert StockTransfer.objects.filter(pk=transfer.pk).exists()


def test_origin_transfer_cannot_be_repointed_through_model_save(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "1", env["cells"][0], env["cells"][1], "round4-immutable")
    target = _lot_at(line, env["cells"][1])
    target.origin_transfer_id = None
    with pytest.raises(ValidationError):
        target.save(update_fields=["origin_transfer"])


def test_received_sale_lot_reset_to_receiving_does_not_reopen_capacity(env):
    line = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _sell(env, lot, "2")
    StockLot.objects.filter(pk=lot.pk).update(status=StockLot.Status.RECEIVING)

    assert _cls(line, lot) == (PRIMARY_RECEIPT, Decimal("10"))
    assert remaining_qty(line) == Decimal("0")


def test_received_writeoff_lot_reset_to_receiving_does_not_reopen_capacity(env):
    line = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    document = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=env["admin"])
    add_stock_lot_to_write_off(document, lot, Decimal("2"))
    complete_write_off(document, by=env["admin"])
    StockLot.objects.filter(pk=lot.pk).update(status=StockLot.Status.RECEIVING)

    assert _cls(line, lot) == (PRIMARY_RECEIPT, Decimal("10"))
    assert remaining_qty(line) == Decimal("0")


def test_received_transfer_source_reset_to_receiving_does_not_reopen_capacity(env):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-status-transfer")
    target = _lot_at(line, env["cells"][1])
    StockLot.objects.filter(pk=source.pk).update(status=StockLot.Status.RECEIVING)

    assert _cls(line, source) == (PRIMARY_RECEIPT, Decimal("10"))
    assert _cls(line, target) == (TRANSFER_DERIVED, Decimal("0"))
    assert remaining_qty(line) == Decimal("0")


def test_receiving_status_conflicting_with_unproven_movement_is_unknown(env):
    line = _finalized_line(env, env["part"], "10")
    lot = _flip(create_stock_lot(line, env["cells"][0], Decimal("6")))
    adjust_stock_lot_quantity(lot, Decimal("-1"), comment="Сверка")
    StockLot.objects.filter(pk=lot.pk).update(status=StockLot.Status.RECEIVING)

    assert _cls(line, lot) == (UNKNOWN, None)
    assert remaining_qty(line) == Decimal("0")


# --- B. LEGACY_PRIMARY: false positives must not happen ------------------------------------


def _age(lot, seconds):
    StockLot.objects.filter(pk=lot.pk).update(
        created_at=lot.created_at - timedelta(seconds=seconds)
    )


def test_a_transfer_target_with_broken_evidence_is_unknown_not_legacy(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-f1")
    target = _lot_at(line, env["cells"][1])
    _age(target, 60)  # timestamps are not the creating-transfer identity

    assert _cls(line, target)[0] == TRANSFER_DERIVED


def test_a_return_lot_with_broken_evidence_is_unknown_not_legacy(env):
    line = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = _sell(env, lot, "2")
    stock_return = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        stock_return, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("2"),
        to_location=env["cells"][1], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(stock_return, by=env["admin"])
    returned = _lot_at(line, env["cells"][1])
    assert _cls(line, returned)[0] == RETURN_DERIVED
    _age(returned, 60)

    assert _cls(line, returned)[0] == RETURN_DERIVED


def test_a_merge_into_a_young_legacy_lot_is_not_mistaken_for_its_creation(env):
    """Worst case for the transfer rule: a status-flipped lot receives a transfer
    of exactly its own quantity right after creation."""
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("4")))
    young = _flip(create_stock_lot(line, env["cells"][1], Decimal("2")))
    transfer = _transfer(env, "2", env["cells"][0], env["cells"][1], "adv-f2")  # merges
    StockLot.objects.filter(pk=young.pk).update(note=f"Перемещение #{transfer.pk} из X")

    provenance, intake = _cls(line, young)
    assert provenance != TRANSFER_DERIVED
    assert _cls(line, source) == (PRIMARY_RECEIPT, Decimal("4"))
    assert remaining_qty(line) == Decimal("0")  # corrupted provenance fails closed


# --- C. TRANSFER_DERIVED -------------------------------------------------------------------


def test_transfer_targets_through_partials_merges_and_round_trips(env):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "2", env["cells"][0], env["cells"][1], "adv-t1")  # creates
    _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-t2")  # merges
    _transfer(env, "1", env["cells"][1], env["cells"][0], "adv-t3")  # back into source
    _transfer(env, "4", env["cells"][1], env["cells"][2], "adv-t4")  # creates another
    target = _lot_at(line, env["cells"][1])
    second = _lot_at(line, env["cells"][2])
    StockLot.objects.filter(pk__in=[target.pk, second.pk]).update(note="")

    assert _cls(line, source) == (PRIMARY_RECEIPT, Decimal("10"))
    assert _cls(line, target) == (TRANSFER_DERIVED, Decimal("0"))
    assert _cls(line, second) == (TRANSFER_DERIVED, Decimal("0"))
    assert remaining_qty(line) == Decimal("0")


def test_transfer_split_targets_match_their_own_source_portions(env):
    first_line = _finalized_line(env, env["part"], "5")
    second_line = _finalized_line(env, env["part"], "5")
    receive_stock_lot(create_stock_lot(first_line, env["cells"][0], Decimal("1")))
    receive_stock_lot(create_stock_lot(second_line, env["cells"][0], Decimal("2")))

    _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-split-transfer")

    first_target = StockLot.objects.get(batch_line=first_line, location=env["cells"][1])
    second_target = StockLot.objects.get(batch_line=second_line, location=env["cells"][1])
    assert first_target.initial_quantity == Decimal("1")
    assert second_target.initial_quantity == Decimal("2")
    assert _cls(first_line, first_target) == (TRANSFER_DERIVED, Decimal("0"))
    assert _cls(second_line, second_target) == (TRANSFER_DERIVED, Decimal("0"))
    assert remaining_qty(first_line) == Decimal("4")
    assert remaining_qty(second_line) == Decimal("3")


def test_a_transfer_movement_needs_its_stock_transfer_row(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-t5")
    target = _lot_at(line, env["cells"][1])
    # A movement pointing at another transfer's id or cell is not evidence.
    StockTransfer.objects.filter(pk=transfer.pk).update(to_location=env["cells"][3])

    assert _cls(line, target)[0] == UNKNOWN


def test_a_transfer_movement_without_its_document_is_unknown(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-no-document")
    target = _lot_at(line, env["cells"][1])
    from django.db.models.deletion import ProtectedError

    with pytest.raises(ProtectedError):
        StockTransfer.objects.filter(pk=transfer.pk).delete()

    assert _cls(line, target)[0] == TRANSFER_DERIVED
    assert StockTransfer.objects.filter(pk=transfer.pk).exists()


@pytest.mark.parametrize(
    "replacement",
    [StockMovement.MovementType.ADJUST_OUT, StockMovement.MovementType.SALE_LOT],
)
def test_damaged_transfer_movement_cannot_fall_through_to_legacy(env, replacement):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], f"adv-damaged-{replacement}")
    target = _lot_at(line, env["cells"][1])
    StockMovement.objects.filter(
        document_type="stock_transfer", document_id=transfer.pk
    ).update(movement_type=replacement)

    assert _cls(line, target) == (UNKNOWN, None)
    assert remaining_qty(line) == Decimal("0")


def test_missing_transfer_movement_keeps_target_unknown(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-missing-move")
    target = _lot_at(line, env["cells"][1])
    StockMovement.objects.filter(
        document_type="stock_transfer", document_id=transfer.pk
    ).delete()

    assert _cls(line, target) == (UNKNOWN, None)
    assert remaining_qty(line) == Decimal("0")


def test_incomplete_and_conflicting_transfer_evidence_fail_closed(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-incomplete")
    target = _lot_at(line, env["cells"][1])
    StockTransfer.objects.filter(pk=transfer.pk).update(quantity=Decimal("4"))
    assert _cls(line, target) == (UNKNOWN, None)

    # A second nearby transfer into the same target cell is conflicting creation evidence.
    conflicting, _created = perform_stock_transfer(
        part=env["part"], from_location=env["cells"][0], to_location=env["cells"][1],
        quantity="1", stock_state=StockLot.Status.AVAILABLE, token="adv-conflicting",
    )
    StockTransfer.objects.filter(pk=conflicting.pk).update(created_at=target.created_at)
    StockMovement.objects.filter(
        document_type="stock_transfer", document_id=conflicting.pk
    ).update(created_at=target.created_at + timedelta(milliseconds=100))
    assert _cls(line, target) == (UNKNOWN, None)


def test_legacy_lot_on_non_unique_same_batch_line_is_unknown(env):
    line = _finalized_line(env, env["part"], "10")
    duplicate = line.__class__.objects.create(
        batch=line.batch,
        part_type=line.part_type,
        quantity=Decimal("10"),
        unit_cost_currency=Decimal("1"),
    )
    lot = _flip(create_stock_lot(line, env["cells"][0], Decimal("6")))

    assert _cls(line, lot) == (UNKNOWN, None)
    assert remaining_qty(line) == Decimal("0")
    assert duplicate.part_type_id == lot.part_type_id


def test_a_transfer_target_rebound_to_another_line_is_unknown(env):
    original = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(original, env["cells"][0], Decimal("10")))
    _transfer(env, "2", env["cells"][0], env["cells"][1], "adv-rebind-target")
    target = _lot_at(original, env["cells"][1])
    destination_line = _finalized_line(env, env["part"], "10")
    StockLot.objects.filter(pk=target.pk).update(batch_line=destination_line)

    assert _cls(destination_line, target) == (UNKNOWN, None)
    assert _cls(original, source) == (PRIMARY_RECEIPT, Decimal("10"))
    assert remaining_qty(original) == Decimal("0")
    assert remaining_qty(destination_line) == Decimal("0")


@pytest.mark.parametrize("movement_kind", ["sale", "adjustment", "writeoff"])
@pytest.mark.parametrize("rebind", ["batch_line_only", "batch_and_line", "same_batch_line"])
def test_rebound_legacy_lot_with_origin_movements_is_unknown_and_closes_both_lines(
    env, movement_kind, rebind
):
    origin, lot = _legacy_line(env)
    if movement_kind == "sale":
        _sell(env, lot, "1")
    elif movement_kind == "adjustment":
        adjust_stock_lot_quantity(lot, Decimal("1"), comment="Сверка +")
        adjust_stock_lot_quantity(lot, Decimal("-1"), comment="Сверка -")
    else:
        document = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=env["admin"])
        add_stock_lot_to_write_off(document, lot, Decimal("1"))
        complete_write_off(document, by=env["admin"])

    if rebind == "same_batch_line":
        destination = origin.__class__.objects.create(
            batch=origin.batch,
            part_type=origin.part_type,
            quantity=Decimal("10"),
            unit_cost_currency=Decimal("1"),
        )
        # Same-batch duplicate lines are themselves ambiguous; the movement
        # mismatch must still be treated as non-provenance.
        StockLot.objects.filter(pk=lot.pk).update(batch_line=destination)
    else:
        destination = _finalized_line(env, env["part"], "10")
        updates = {"batch_line": destination}
        if rebind == "batch_and_line":
            updates["batch"] = destination.batch
        StockLot.objects.filter(pk=lot.pk).update(**updates)

    lot.refresh_from_db()
    assert _cls(destination, lot) == (UNKNOWN, None)
    assert remaining_qty(destination) == Decimal("0")  # no phantom intake
    assert remaining_qty(origin) == Decimal("0")  # no reopened source capacity
    assert StockMovement.objects.filter(stock_lot=lot, batch_line=origin).exists()


def test_rebound_legacy_lot_batch_only_keeps_origin_line_closed(env):
    origin, lot = _legacy_line(env)
    adjust_stock_lot_quantity(lot, Decimal("1"), comment="Сверка +")
    adjust_stock_lot_quantity(lot, Decimal("-1"), comment="Сверка -")
    destination = _finalized_line(env, env["part"], "10")
    StockLot.objects.filter(pk=lot.pk).update(batch=destination.batch)

    lot.refresh_from_db()
    assert _cls(origin, lot) == (UNKNOWN, None)
    assert remaining_qty(origin) == Decimal("0")
    assert remaining_qty(destination) == Decimal("10")


def test_untouched_legacy_primary_still_proves_its_intake(env):
    line, lot = _legacy_line(env)

    assert _cls(line, lot) == (LEGACY_PRIMARY, Decimal("6"))
    assert remaining_qty(line) == Decimal("4")


@pytest.mark.parametrize(
    "damage",
    [
        "target_clock",
        "document_clock",
        "repointed_document",
        "retyped_movement_and_deleted_document",
        "deleted_movement_and_document",
    ],
)
def test_damaged_transfer_evidence_never_upgrades_target_to_legacy(env, damage):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], f"f1-{damage}")
    target = _lot_at(line, env["cells"][1])
    move = StockMovement.objects.get(document_type="stock_transfer", document_id=transfer.pk)

    if damage == "target_clock":
        _age(target, 60)
    elif damage == "document_clock":
        StockTransfer.objects.filter(pk=transfer.pk).update(
            created_at=transfer.created_at + timedelta(seconds=60)
        )
        StockMovement.objects.filter(pk=move.pk).update(
            created_at=move.created_at + timedelta(seconds=120)
        )
    elif damage == "repointed_document":
        StockTransfer.objects.filter(pk=transfer.pk).update(to_location=env["cells"][2])
        StockMovement.objects.filter(pk=move.pk).update(to_location=env["cells"][2])
    elif damage == "retyped_movement_and_deleted_document":
        StockMovement.objects.filter(pk=move.pk).update(
            movement_type=StockMovement.MovementType.ADJUST_OUT,
            document_type="",
            document_id=None,
        )
        _corrupt_lot_for_adversarial_test(target, origin_transfer=None, creation_origin=None)
        StockTransfer.objects.filter(pk=transfer.pk)._raw_delete(using="default")
    else:
        StockMovement.objects.filter(pk=move.pk).delete()
        _corrupt_lot_for_adversarial_test(target, origin_transfer=None, creation_origin=None)
        StockTransfer.objects.filter(pk=transfer.pk)._raw_delete(using="default")

    expected = TRANSFER_DERIVED if damage in {"target_clock", "document_clock"} else UNKNOWN
    assert _cls(line, target)[0] == expected
    if expected == UNKNOWN:
        assert _cls(line, target)[1] is None
    assert remaining_qty(line) == Decimal("0")
    source.refresh_from_db()
    assert source.quantity == Decimal("7")


def test_historical_unlinked_transfer_outside_clock_window_fails_closed(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "f1-clock-outside")
    target = _lot_at(line, env["cells"][1])
    move = StockMovement.objects.get(document_type="stock_transfer", document_id=transfer.pk)
    # Simulate a historical row without the new direct FK and a damaged clock
    # ordering that would make the old initial-quantity reconstruction look
    # like a supplier lot. The transfer-shaped movement must block that fallback.
    _corrupt_lot_for_adversarial_test(
        target, origin_transfer=None, creation_origin=None, note=""
    )
    StockTransfer.objects.filter(pk=transfer.pk).update(
        created_at=target.created_at + timedelta(seconds=3)
    )
    StockMovement.objects.filter(pk=move.pk).update(
        created_at=target.created_at - timedelta(seconds=3)
    )

    assert _cls(line, target) == (UNKNOWN, None)
    assert remaining_qty(line) == Decimal("0")


def test_two_same_part_lines_in_one_batch_keep_exact_transfer_provenance(env):
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    first = BatchLine.objects.create(
        batch=batch,
        part_type=env["part"],
        quantity=Decimal("10"),
        unit_cost_currency=Decimal("1"),
    )
    second = BatchLine.objects.create(
        batch=batch,
        part_type=env["part"],
        quantity=Decimal("5"),
        unit_cost_currency=Decimal("1"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    from apps.procurement.services import finalize_cost

    finalize_cost(batch, env["admin"])
    first.refresh_from_db()
    second.refresh_from_db()
    source_a = receive_stock_lot(create_stock_lot(first, env["cells"][0], Decimal("4")))
    source_b = receive_stock_lot(create_stock_lot(second, env["cells"][0], Decimal("3")))

    _transfer(env, "5", env["cells"][0], env["cells"][1], "f1-same-batch-same-part")

    target_a = _lot_at(first, env["cells"][1])
    target_b = _lot_at(second, env["cells"][1])
    assert _cls(first, target_a) == (TRANSFER_DERIVED, Decimal("0"))
    assert _cls(second, target_b) == (TRANSFER_DERIVED, Decimal("0"))
    assert remaining_qty(first) == Decimal("6")
    assert remaining_qty(second) == Decimal("2")
    source_a.refresh_from_db()
    source_b.refresh_from_db()
    assert source_a.quantity == Decimal("0")
    assert source_b.quantity == Decimal("2")


@pytest.mark.parametrize(
    "damage",
    [
        "missing_move", "retyped_move", "document_link_cleared", "missing_document",
        "wrong_batch_line", "wrong_part", "wrong_cell", "wrong_quantity",
        "draft_return", "completion_time_cleared", "returned_lot_changed",
        "multiple_returns",
    ],
)
@pytest.mark.parametrize("draft_delay_seconds", [0, 600])
def test_damaged_return_evidence_never_upgrades_target_to_legacy(
    env, damage, draft_delay_seconds
):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = _sell(env, source, "2")
    stock_return = create_return(source=sale, reason="Возврат", by=env["admin"])
    return_line = add_sale_line_return(
        stock_return,
        Sale.objects.get(pk=sale.pk).lines.get(),
        Decimal("2"),
        to_location=env["cells"][1],
        restock_status=StockLot.Status.AVAILABLE,
    )
    if draft_delay_seconds:
        StockReturnLine.objects.filter(pk=return_line.pk).update(
            created_at=timezone.now() - timedelta(seconds=draft_delay_seconds)
        )
    complete_return(stock_return, by=env["admin"])
    return_line = stock_return.lines.get()
    target = return_line.returned_lot
    movement = StockMovement.objects.get(
        stock_lot=target, movement_type=StockMovement.MovementType.RETURN_LOT
    )
    _corrupt_lot_for_adversarial_test(target, origin_return_line=None, creation_origin=None)

    if damage == "missing_move":
        StockMovement.objects.filter(pk=movement.pk).delete()
    elif damage == "document_link_cleared":
        StockMovement.objects.filter(pk=movement.pk).update(document_id=None)
    elif damage == "retyped_move":
        StockMovement.objects.filter(pk=movement.pk).update(
            movement_type=StockMovement.MovementType.MOVE_LOT,
            document_type="",
            document_id=None,
        )
    elif damage == "missing_document":
        _erase_return_document_for_adversarial_test(stock_return)
    elif damage == "wrong_batch_line":
        other_line = _finalized_line(env, env["part"], "10")
        StockReturnLine.objects.filter(pk=return_line.pk).update(
            batch=other_line.batch, batch_line=other_line,
        )
    elif damage == "wrong_part":
        other_part = env["part"].__class__.objects.create(
            name="Другая деталь", category=env["part"].category, unit=env["part"].unit,
            tracking_mode=env["part"].tracking_mode,
        )
        StockReturnLine.objects.filter(pk=return_line.pk).update(part_type=other_part)
    elif damage == "wrong_cell":
        StockReturnLine.objects.filter(pk=return_line.pk).update(to_location=env["cells"][2])
    elif damage == "wrong_quantity":
        StockReturnLine.objects.filter(pk=return_line.pk).update(quantity=Decimal("1"))
    elif damage == "draft_return":
        StockReturn.objects.filter(pk=stock_return.pk).update(
            status=StockReturn.Status.DRAFT, completed_at=None,
        )
    elif damage == "completion_time_cleared":
        StockReturn.objects.filter(pk=stock_return.pk).update(completed_at=None)
    elif damage == "returned_lot_changed":
        decoy_line = _finalized_line(env, env["part"], "10")
        decoy = create_stock_lot(decoy_line, env["cells"][2], Decimal("2"))
        StockReturnLine.objects.filter(pk=return_line.pk).update(returned_lot=decoy)
    else:
        duplicate = StockReturnLine.objects.get(pk=return_line.pk)
        duplicate.pk = None
        duplicate.save()
    expected = UNKNOWN

    assert _cls(line, target)[0] == expected
    assert remaining_qty(line) == Decimal("0")


@pytest.mark.parametrize("rebound", ["movement", "source", "target", "all"])
def test_same_batch_same_part_batchline_rebind_breaks_transfer_lineage(env, rebound):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], f"adv-line-rebind-{rebound}")
    target = _lot_at(line, env["cells"][1])
    replacement = line.__class__.objects.create(
        batch=line.batch,
        part_type=line.part_type,
        quantity=Decimal("10"),
        unit_cost_currency=Decimal("1"),
    )
    move_query = StockMovement.objects.filter(
        document_type="stock_transfer", document_id=transfer.pk
    )
    if rebound in {"movement", "all"}:
        move_query.update(batch_line=replacement)
    if rebound in {"source", "all"}:
        StockLot.objects.filter(pk=source.pk).update(batch_line=replacement)
    if rebound in {"target", "all"}:
        StockLot.objects.filter(pk=target.pk).update(batch_line=replacement)

    target.refresh_from_db()
    assert _cls(target.batch_line, target) == (UNKNOWN, None)


@pytest.mark.parametrize(
    "tamper",
    ["move_part", "source_part", "target_part", "move_batchline"],
)
def test_transfer_identity_mismatch_is_unknown(env, public_catalog, tamper):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], f"adv-id-{tamper}")
    target = _lot_at(line, env["cells"][1])
    other_part = public_catalog.part("Другая деталь", article="ADV-2", price="100")
    other_line = _finalized_line(env, other_part, "10")
    move = StockMovement.objects.get(document_type="stock_transfer", document_id=transfer.pk)

    if tamper == "move_part":
        StockMovement.objects.filter(pk=move.pk).update(part_type=other_part)
    elif tamper == "source_part":
        StockLot.objects.filter(pk=source.pk).update(part_type=other_part)
    elif tamper == "target_part":
        StockLot.objects.filter(pk=target.pk).update(part_type=other_part)
    else:
        StockMovement.objects.filter(pk=move.pk).update(batch_line=other_line)

    assert _cls(line, target)[0] == UNKNOWN
    assert remaining_qty(line) == Decimal("0")


# --- D. Found stock never touches a supplier line --------------------------------------------


def test_found_stock_neither_consumes_nor_reopens_a_supplier_line(env):
    supplier = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(create_stock_lot(supplier, env["cells"][0], Decimal("10")))
    post_found_stock_group(
        entries=[{"source": "warehouse", "source_id": env["part"].pk,
                  "exact_number": "ADV-1", "quantity": 2}],
        location=env["cells"][0], token="adv-found-1",
    )  # adds onto the existing supplier lot
    add_found_stock(env["part"], env["cells"][0], Decimal("1"))
    assert _cls(supplier, lot) == (PRIMARY_RECEIPT, Decimal("10"))
    assert remaining_qty(supplier) == Decimal("0")


def test_a_found_line_counts_only_its_own_posting(env):
    post_found_stock_group(
        entries=[{"source": "warehouse", "source_id": env["part"].pk,
                  "exact_number": "ADV-1", "quantity": 3}],
        location=env["cells"][4], token="adv-found-2",
    )
    lot = StockLot.objects.get(part_type=env["part"], location=env["cells"][4])
    add_found_stock(env["part"], env["cells"][4], Decimal("2"))  # later, onto the same lot

    assert _cls(lot.batch_line, lot) == (FOUND_STOCK, Decimal("3"))
    assert remaining_qty(lot.batch_line) == Decimal("0")


# --- E. Pending ---------------------------------------------------------------------------


def test_a_pending_lot_is_received_exactly_once(env):
    line = _finalized_line(env, env["part"], "10")
    lot = create_stock_lot(line, env["cells"][0], Decimal("4"))
    assert _cls(line, lot) == (PENDING_RECEIPT, Decimal("4"))
    receive_stock_lot(lot)
    assert StockMovement.objects.filter(stock_lot=lot, movement_type=RECEIPT).count() == 1
    assert _cls(line, lot) == (PRIMARY_RECEIPT, Decimal("4"))
    assert remaining_qty(line) == Decimal("6")


# --- F. History written by the old backfill, if it ever ran ---------------------------------


def _old_backfill_receipt(lot):
    """What backfill_opening_movements wrote before eade473."""
    StockMovement.objects.create(
        movement_type=RECEIPT, part_type=lot.part_type, stock_lot=lot, batch=lot.batch,
        batch_line=lot.batch_line, to_location=lot.location, quantity=lot.quantity,
        unit_cost_rub=lot.landed_unit_cost_rub, comment="Открывающий остаток",
    )


def test_an_old_backfill_receipt_is_not_evidence(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "adv-b1")
    target = _lot_at(line, env["cells"][1])
    _old_backfill_receipt(target)

    assert _cls(line, target) == (TRANSFER_DERIVED, Decimal("0"))
    assert remaining_qty(line) == Decimal("0")


def test_an_old_backfill_receipt_on_a_pending_lot_is_not_counted_twice(env):
    line = _finalized_line(env, env["part"], "10")
    lot = create_stock_lot(line, env["cells"][0], Decimal("4"))
    _old_backfill_receipt(lot)
    receive_stock_lot(lot)  # the real receipt

    assert _cls(line, lot) == (PRIMARY_RECEIPT, Decimal("4"))
    assert remaining_qty(line) == Decimal("6")


# --- G. Lots moved to another batch line in admin (possible before 2c64484) --------------


def test_a_lot_reassigned_to_another_line_keeps_its_intake_on_the_original_line(env):
    original = _finalized_line(env, env["part"], "10")
    other = _finalized_line(env, env["part"], "10")
    lot = receive_stock_lot(create_stock_lot(original, env["cells"][0], Decimal("10")))
    StockLot.objects.filter(pk=lot.pk).update(batch_line=other)  # pre-2c64484 admin edit

    assert remaining_qty(original) == Decimal("0")  # the receipt still belongs here
    assert remaining_qty(other) == Decimal("10")  # nothing was received from this one


# --- H. Property test: random histories against an oracle -----------------------------------


@pytest.mark.parametrize("seed", range(12))
def test_random_histories_never_lose_or_invent_intake(env, seed):
    rng = random.Random(seed)
    line = _finalized_line(env, env["part"], "20")
    cells = env["cells"]
    received = Decimal("0")
    tokens = iter(range(10_000))
    for _step in range(14):
        lots = list(StockLot.objects.filter(batch_line=line, status=StockLot.Status.AVAILABLE,
                                            quantity__gt=0))
        op = rng.choice(["receive", "sell", "transfer", "adjust", "writeoff", "move"])
        try:
            if op == "receive" or not lots:
                free = [c for c in cells if not StockLot.objects.filter(
                    batch_line=line, location=c).exists()]
                room = int(remaining_qty(line))
                if not free or room == 0:
                    continue
                quantity = rng.randint(1, min(room, 5))
                lot = create_stock_lot(line, rng.choice(free), Decimal(quantity))
                if rng.random() < 0.3:
                    _flip(lot)  # some arrive the pre-fix way
                else:
                    receive_stock_lot(lot)
                received += quantity
            elif op == "sell":
                lot = rng.choice(lots)
                _sell(env, lot, str(rng.randint(1, int(lot.quantity))))
            elif op == "transfer":
                lot = rng.choice(lots)
                target = rng.choice([c for c in cells if c.pk != lot.location_id])
                _transfer(env, str(rng.randint(1, int(lot.quantity))), lot.location, target,
                          f"prop-{seed}-{next(tokens)}")
            elif op == "adjust":
                lot = rng.choice(lots)
                adjust_stock_lot_quantity(lot, Decimal(rng.choice([1, -1])), comment="Пересчёт")
            elif op == "writeoff":
                lot = rng.choice(lots)
                doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=env["admin"])
                add_stock_lot_to_write_off(doc, lot, Decimal("1"))
                complete_write_off(doc, by=env["admin"])
                if rng.random() < 0.5:
                    cancel_write_off(doc, by=env["admin"])
            else:
                lot = rng.choice(lots)
                move_stock_lot(lot, rng.choice([c for c in cells if c.pk != lot.location_id]))
        except (InventoryError, ValidationError, Exception) as exc:  # occupied cell etc.
            if exc.__class__.__name__ not in {
                "InventoryError", "SaleError", "WriteOffError", "IntegrityError",
            }:
                raise
        rows = line_provenance(line)
        assert all(row.provenance != UNKNOWN for row in rows), rows
        assert sum(row.intake for row in rows) == received
        assert remaining_qty(line) == Decimal("20") - received
