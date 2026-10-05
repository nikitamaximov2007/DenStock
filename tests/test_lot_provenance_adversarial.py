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
from apps.returns.services import add_sale_line_return, complete_return, create_return
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


# --- B. LEGACY_PRIMARY: true positives through every kind of later history ------------


def _legacy_line(env, quantity="6"):
    line = _finalized_line(env, env["part"], "10")
    return line, _flip(create_stock_lot(line, env["cells"][0], Decimal(quantity)))


def test_legacy_survives_sale_return_cancellation_adjust_writeoff_and_moves(env):
    line, lot = _legacy_line(env)
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
    _age(target, 60)  # evidence no longer in one transaction

    assert _cls(line, target)[0] == UNKNOWN


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

    assert _cls(line, returned)[0] == UNKNOWN


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
    assert remaining_qty(line) == Decimal("4")  # 4 + 2 received, never 4 + 0


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
    StockTransfer.objects.filter(pk=transfer.pk).delete()

    assert _cls(line, target)[0] == UNKNOWN


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
