"""Explicit transfer links outrank notes; historical links require exact notes."""

from decimal import Decimal

import pytest
from django.db import connection
from django.urls import reverse

from apps.inventory.lot_provenance import TRANSFER_DERIVED, UNKNOWN
from apps.inventory.models import StockLot, StockTransfer
from apps.inventory.services import create_stock_lot, receive_stock_lot
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


def _transfer_target(env):
    line = _finalized_line(env, env["part"], "10")
    receive_stock_lot(create_stock_lot(line, env["cells"][0], Decimal("10")))
    transfer = _transfer(env, "3", env["cells"][0], env["cells"][1], "r8-note")
    target = StockLot.objects.get(batch_line=line, location=env["cells"][1])
    return line, transfer, target


def _note(case, transfer):
    canonical = f"Перемещение #{transfer.pk} из {transfer.from_location_code}"
    return {
        "canonical": canonical,
        "empty": "",
        "arbitrary": "Заметка сотрудника",
        "extra_prefix": "X " + canonical,
        "truncated": f"Перемещение #{transfer.pk} из",
        "extra_suffix": canonical + " extra",
        "edited_cell": f"Перемещение #{transfer.pk} из S99-D99-C99",
        "wrong_id": f"Перемещение #{transfer.pk + 1} из {transfer.from_location_code}",
        "unicode_id": "Перемещение #" + "".join(
            chr(ord("٠") + int(digit)) for digit in str(transfer.pk)
        ) + f" из {transfer.from_location_code}",
        "space": f"Перемещение # {transfer.pk} из {transfer.from_location_code}",
    }[case]


@pytest.mark.parametrize("mode", ["explicit", "historical"])
@pytest.mark.parametrize("case", [
    "canonical", "empty", "arbitrary", "extra_prefix", "truncated", "extra_suffix",
    "edited_cell", "wrong_id", "unicode_id", "space",
])
def test_admin_note_edit_keeps_only_proven_transfer_origin(env, client, mode, case):
    line, transfer, target = _transfer_target(env)
    if mode == "historical":
        _corrupt_lot_for_adversarial_test(
            target, origin_transfer=None, creation_origin=None
        )
        target.refresh_from_db()
        assert target.origin_transfer_id is None
    else:
        assert target.origin_transfer_id == transfer.pk
    note = _note(case, transfer)
    client.force_login(env["admin"])
    response = client.post(
        reverse("admin:inventory_stocklot_change", args=[target.pk]),
        {"note": note, "_save": "Сохранить"},
    )
    assert response.status_code == 302
    target.refresh_from_db()
    assert target.note == note
    expected = TRANSFER_DERIVED if mode == "explicit" or case == "canonical" else UNKNOWN
    assert _cls(line, target)[0] == _sql_class(target) == expected


@pytest.mark.parametrize("case", ["canonical", "empty", "wrong_id"])
def test_broken_structured_transfer_stays_unknown_regardless_of_note(env, case):
    line, transfer, target = _transfer_target(env)
    target.note = _note(case, transfer)
    target.save(update_fields=["note"])
    StockTransfer.objects.filter(pk=transfer.pk).update(quantity=Decimal("4"))
    assert target.origin_transfer_id == transfer.pk
    assert _cls(line, target)[0] == _sql_class(target) == UNKNOWN
