"""Lots without a RECEIVE_LOT movement, classified from ledger evidence only.

Each case reproduces how production got such a lot: a transfer target (its
MOVE_LOT is on the source lot), a return, a recount, found stock, and a lot
received before 108b5ad by flipping its status without receive_stock_lot.
Nothing is guessed: a lot whose ledger cannot rebuild its intake stays
UNKNOWN and closes intake on its line. Nothing is written.
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.urls import reverse

from apps.inventory.lot_provenance import (
    FOUND_STOCK,
    LEGACY_PRIMARY,
    PENDING_RECEIPT,
    PRIMARY_RECEIPT,
    RECOUNT_DERIVED,
    RETURN_DERIVED,
    TRANSFER_DERIVED,
    UNKNOWN,
    line_provenance,
)
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    adjust_stock_lot_quantity,
    backfill_opening_movements,
    create_stock_lot,
    get_or_create_section_recount_lot,
    move_stock_lot,
    perform_stock_transfer,
    post_found_stock_group,
    receive_stock_lot,
    remaining_qty,
)
from apps.procurement.models import BatchLine
from apps.returns.models import StockReturn
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.models import Sale
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.warehouse.models import StorageLocation
from tests.customs_support import remember_customs
from tests.test_piece_stock_boundary import _finalized_line, stock  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def final_sql_class_parity():
    """Cover every established provenance class with final SQL parity on PG16."""
    yield
    if connection.vendor == "postgresql":
        from tests.lot_provenance_sql_parity import assert_final_provenance_parity

        assert_final_provenance_parity()


@pytest.fixture
def env(stock, public_catalog):  # noqa: F811
    cells = [
        StorageLocation.objects.create(
            name=f"Prov {n}", code=f"S09-D04-C0{n}", storage_allowed=True, is_active=True
        )
        for n in range(1, 5)
    ]
    part = public_catalog.part("Ремень", article="PROV-1", price="100")
    remember_customs(part)
    return {**stock, "cells": cells, "part": part}


def _classes(line):
    return {lot.lot_id: (lot.provenance, lot.intake) for lot in line_provenance(line)}


def _transfer(env, quantity, source, target, token):
    return perform_stock_transfer(
        part=env["part"], from_location=source, to_location=target, quantity=quantity,
        stock_state=StockLot.Status.AVAILABLE, token=token,
    )


def _status_flip(lot):
    """What the lot status button did before 108b5ad: no receive_stock_lot."""
    StockLot.objects.filter(pk=lot.pk).update(status=StockLot.Status.AVAILABLE)
    lot.refresh_from_db()
    return lot


def _sell(env, lot, quantity):
    sale = create_sale(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_sale(sale, lot, Decimal(quantity), unit_price=Decimal("100"))
    return complete_sale(sale, by=env["admin"])


def test_an_untouched_transfer_target_has_no_movement_and_takes_no_capacity(env):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "prov-t1")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])

    assert not StockMovement.objects.filter(stock_lot=target).exists()  # the production case
    assert target.status == StockLot.Status.AVAILABLE
    StockLot.objects.filter(pk=target.pk).update(note="")  # notes are not evidence
    classes = _classes(line)
    assert classes[target.pk] == (TRANSFER_DERIVED, Decimal("0"))
    assert classes[source.pk] == (PRIMARY_RECEIPT, Decimal("10"))
    assert remaining_qty(line) == Decimal("0")


def test_a_transfer_target_moved_away_later_is_still_recognised(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("6")))
    _transfer(env, "2", env["cells"][0], env["cells"][1], "prov-t2")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    move_stock_lot(target, env["cells"][2])

    assert _classes(line)[target.pk] == (TRANSFER_DERIVED, Decimal("0"))
    assert remaining_qty(line) == Decimal("4")


def test_return_recount_and_found_lots_are_classified_by_their_movements(env):
    line = _finalized_line(env, env["part"], "10")
    source = receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    sale = _sell(env, source, "2")
    stock_return = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        stock_return, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=env["cells"][1], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(stock_return, by=env["admin"])
    recount = get_or_create_section_recount_lot(
        line, env["cells"][2], lot_status=StockLot.Status.AVAILABLE
    )
    adjust_stock_lot_quantity(
        recount, Decimal("2"), comment="Пересчёт", document_type="section_recount"
    )

    classes = _classes(line)
    returned = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    assert classes[returned.pk] == (RETURN_DERIVED, Decimal("0"))
    assert returned.origin_return_line_id == stock_return.lines.get().pk
    assert classes[recount.pk] == (RECOUNT_DERIVED, Decimal("0"))
    assert remaining_qty(line) == Decimal("0")


def test_a_found_stock_line_is_fully_taken_by_its_found_addition(env):
    post_found_stock_group(
        entries=[{"source": "warehouse", "source_id": env["part"].pk,
                  "exact_number": "PROV-1", "quantity": 3}],
        location=env["cells"][3], token="prov-found",
    )
    lot = StockLot.objects.get(part_type=env["part"], location=env["cells"][3])
    line = lot.batch_line

    assert _classes(line)[lot.pk] == (FOUND_STOCK, Decimal("3"))
    assert remaining_qty(line) == Decimal("0")  # 0ba1416 left 3 open here
    with pytest.raises(InventoryError, match="можно принять ещё 0"):
        create_stock_lot(line, env["cells"][0], Decimal("3"))


@pytest.mark.parametrize("origin", ["found", "recount"])
@pytest.mark.parametrize("pre_marker", [False, True])
def test_later_return_at_creation_time_keeps_found_or_recount_origin(
    env, origin, pre_marker
):
    if origin == "found":
        post_found_stock_group(
            entries=[{"source": "warehouse", "source_id": env["part"].pk,
                      "exact_number": "PROV-1", "quantity": 3}],
            location=env["cells"][3], token="prov-found-return",
        )
        lot = StockLot.objects.get(part_type=env["part"], location=env["cells"][3])
        expected = FOUND_STOCK
    else:
        line = _finalized_line(env, env["part"], "3")
        lot = get_or_create_section_recount_lot(
            line, env["cells"][3], lot_status=StockLot.Status.AVAILABLE
        )
        adjust_stock_lot_quantity(
            lot, Decimal("3"), comment="Пересчёт", document_type="section_recount"
        )
        expected = RECOUNT_DERIVED

    if pre_marker:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE inventory_stocklot SET creation_origin = NULL WHERE id = %s",
                [lot.pk],
            )

    sale = _sell(env, lot, "1")
    stock_return = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        stock_return, Sale.objects.get(pk=sale.pk).lines.get(), Decimal("1"),
        to_location=env["cells"][3], restock_status=StockLot.Status.AVAILABLE,
    )
    complete_return(stock_return, by=env["admin"])
    StockReturn.objects.filter(pk=stock_return.pk).update(completed_at=lot.created_at)

    assert _classes(lot.batch_line)[lot.pk][0] == expected


def test_a_legacy_status_flipped_lot_counts_what_the_ledger_proves(env):
    line = _finalized_line(env, env["part"], "10")
    lot = _status_flip(create_stock_lot(line, env["cells"][0], Decimal("6")))
    assert _classes(line)[lot.pk] == (LEGACY_PRIMARY, Decimal("6"))
    assert remaining_qty(line) == Decimal("4")  # not blocked

    _sell(env, lot, "2")  # consumption still never reopens capacity
    _transfer(env, "1", env["cells"][0], env["cells"][1], "prov-t3")
    _transfer(env, "1", env["cells"][1], env["cells"][0], "prov-t4")  # merged back
    assert _classes(line)[lot.pk] == (LEGACY_PRIMARY, Decimal("6"))
    assert remaining_qty(line) == Decimal("4")
    with pytest.raises(InventoryError, match="можно принять ещё 4"):
        create_stock_lot(line, env["cells"][2], Decimal("5"))
    create_stock_lot(line, env["cells"][2], Decimal("4"))
    assert remaining_qty(line) == Decimal("0")


def test_an_untracked_edit_leaves_the_lot_unknown_and_the_line_closed(env):
    line = _finalized_line(env, env["part"], "10")
    lot = _status_flip(create_stock_lot(line, env["cells"][0], Decimal("6")))
    StockLot.objects.filter(pk=lot.pk).update(quantity=Decimal("9"))  # pre-fix edit, no movement

    provenance, intake = _classes(line)[lot.pk]
    assert (provenance, intake) == (UNKNOWN, None)
    assert remaining_qty(line) == Decimal("0")
    with pytest.raises(InventoryError, match="audit_lot_provenance"):
        create_stock_lot(line, env["cells"][1], Decimal("1"))


def test_remaining_route_reports_unknown_provenance_without_zero_quantity_error(
    env, client
):
    line = _finalized_line(env, env["part"], "10")
    lot = _status_flip(create_stock_lot(line, env["cells"][0], Decimal("6")))
    StockLot.objects.filter(pk=lot.pk).update(quantity=Decimal("9"))
    before = (StockLot.objects.count(), StockMovement.objects.count())
    client.force_login(env["admin"])

    url = reverse("lot_create_remaining", args=[line.pk])
    page = client.get(url)
    assert page.status_code == 200
    assert "происхождение которого журнал не доказывает" in page.content.decode()
    assert "остаток для распределения 0" not in page.content.decode()

    response = client.post(
        url, {"location": env["cells"][1].pk, "note": ""}, follow=True
    )
    body = response.content.decode()
    assert response.status_code == 200
    assert "происхождение которого журнал не доказывает" in body
    assert "Количество должно быть больше нуля" not in body
    assert (StockLot.objects.count(), StockMovement.objects.count()) == before
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("9")


def test_regular_lot_route_fails_closed_for_unknown_provenance(env, client):
    line = _finalized_line(env, env["part"], "10")
    lot = _status_flip(create_stock_lot(line, env["cells"][0], Decimal("6")))
    StockLot.objects.filter(pk=lot.pk).update(quantity=Decimal("9"))
    client.force_login(env["admin"])
    before = (StockLot.objects.count(), StockMovement.objects.count())
    url = reverse("lot_create", args=[line.pk])

    for method in (client.get, lambda target: client.post(target, {"broken": "form"})):
        response = method(url)
        body = response.content.decode()
        assert response.status_code == 200
        assert "происхождение которого журнал не доказывает" in body
        assert "остаток для распределения 0" not in body
        assert "Сохранить" not in body
        assert "Количество должно быть больше нуля" not in body

    assert (StockLot.objects.count(), StockMovement.objects.count()) == before
    assert StockLot.objects.get(pk=lot.pk).quantity == Decimal("9")


def test_a_pending_lot_holds_its_quantity(env):
    line = _finalized_line(env, env["part"], "10")
    lot = create_stock_lot(line, env["cells"][0], Decimal("4"))
    assert _classes(line)[lot.pk] == (PENDING_RECEIPT, Decimal("4"))


def test_the_backfill_never_writes_a_receipt_for_a_lot(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "prov-t5")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    pending = create_stock_lot(
        _finalized_line(env, env["part"], "5"), env["cells"][2], Decimal("5")
    )
    flipped = _status_flip(
        create_stock_lot(_finalized_line(env, env["part"], "5"), env["cells"][3], Decimal("5"))
    )
    receipts_before = StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RECEIVE_LOT
    ).count()

    backfill_opening_movements()

    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RECEIVE_LOT
    ).count() == receipts_before
    for lot in (target, pending, flipped):
        assert not StockMovement.objects.filter(stock_lot=lot).exists()
    assert remaining_qty(line) == Decimal("0")


def test_the_audit_reports_classes_and_closed_lines_without_writing(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    _transfer(env, "3", env["cells"][0], env["cells"][1], "prov-t6")
    closed = _finalized_line(env, env["part"], "10")
    unknown = _status_flip(create_stock_lot(closed, env["cells"][2], Decimal("6")))
    StockLot.objects.filter(pk=unknown.pk).update(quantity=Decimal("9"))
    before = (StockMovement.objects.count(), BatchLine.objects.count())

    out = StringIO()
    call_command("audit_lot_provenance", stdout=out)
    text = out.getvalue()

    assert "transfer_derived: 1 / 1 / 1" in text
    assert "primary_receipt: 3 / 3 / 0" in text  # this line's source and the fixture's two
    assert "unknown: 1 / 1 / 1" in text
    assert f"закрыто для приёмки (есть unknown): 1\n  {closed.pk}" in text
    # The old rule (line minus shelf) left 1 open on the unknown line; now 0.
    assert (
        "от прежнего правила (количество минус текущий остаток лотов): 1\n"
        f"  строка {closed.pk}: было 1, теперь 0"
    ) in text
    assert "RECEIVE_LOT старого backfill (не считаются приёмкой): 0" in text
    assert (StockMovement.objects.count(), BatchLine.objects.count()) == before
