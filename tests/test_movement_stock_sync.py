from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import (
    Category,
    Manufacturer,
    PartBarcode,
    PartNumber,
    PartType,
    Unit,
)
from apps.core.models import UnresolvedScan
from apps.counting.models import InventoryCountingLine, InventoryCountingSession
from apps.inventory.models import StockBalance, StockLot, StockMovement, StockTransfer
from apps.inventory.movement import live_stock_rows, movement_sources_for_part
from apps.inventory.services import (
    InventoryError,
    create_part_items,
    create_stock_lot,
    perform_stock_transfer,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

pytestmark = pytest.mark.django_db

ARTICLE = "703500875"
PASSWORD = "test-pass-123"
URL = reverse("scanner_move")


@pytest.fixture
def movement_data(django_user_model):
    admin = django_user_model.objects.create_superuser(
        username="movement-admin", password=PASSWORD
    )
    supplier = Supplier.objects.create(name="Movement supplier")
    category = Category.objects.create(name="Movement category")
    unit = Unit.objects.get(name="Штука")
    manufacturer = Manufacturer.objects.create(name="POLARIS")
    part = PartType.objects.create(
        name="BRAKE PAD",
        category=category,
        unit=unit,
        manufacturer=manufacturer,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=part,
        value=ARTICLE,
        kind=PartNumber.Kind.ARTICLE,
        is_primary=True,
    )
    PartNumber.objects.create(
        part=part,
        value="703500874",
        kind=PartNumber.Kind.ANALOG,
    )
    PartBarcode.objects.create(part=part, value="BAR-703500875")
    source = StorageLocation.objects.create(
        name="Source", code="S03-L03-D02-C04", storage_allowed=True
    )
    target = StorageLocation.objects.create(
        name="Target", code="S03-L03-D02-C03", storage_allowed=True
    )
    third = StorageLocation.objects.create(
        name="Third", code="S03-L03-D02-C02", storage_allowed=True
    )
    return {
        "admin": admin,
        "supplier": supplier,
        "part": part,
        "source": source,
        "target": target,
        "third": third,
    }


def make_line(data, *, quantity="20", suffix=""):
    batch = Batch.objects.create(
        supplier=data["supplier"],
        shipping_cost=Decimal("0"),
        notes=f"movement {suffix}",
    )
    line = BatchLine.objects.create(
        batch=batch,
        part_type=data["part"],
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, data["admin"])
    line.refresh_from_db()
    return line


def make_lot(data, location=None, *, quantity="10", status=StockLot.Status.AVAILABLE, suffix=""):
    line = make_line(data, quantity=quantity, suffix=suffix)
    lot = create_stock_lot(line, location or data["source"], Decimal(quantity))
    if status == StockLot.Status.AVAILABLE:
        receive_stock_lot(lot, by=data["admin"])
    elif status == StockLot.Status.QUARANTINE:
        lot.status = StockLot.Status.QUARANTINE
        lot.save(update_fields=["status"])
        from apps.inventory.services import recompute_balance_row

        recompute_balance_row(line, lot.location)
    lot.refresh_from_db()
    return lot


def make_serial_part(data, *, number="SERIAL-703", placed=True):
    part = PartType.objects.create(
        name="SERIAL SENSOR",
        category=data["part"].category,
        unit=data["part"].unit,
        manufacturer=data["part"].manufacturer,
        tracking_mode=PartType.TrackingMode.SERIAL,
    )
    PartNumber.objects.create(
        part=part, value=number, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    original = data["part"]
    data["part"] = part
    line = make_line(data, quantity="2", suffix=number)
    data["part"] = original
    item = create_part_items(line, 1)[0]
    if placed:
        receive_part_item(item, to_location=data["source"], by=data["admin"])
        item.refresh_from_db()
    return part, item


def transfer(data, *, quantity="3", state=StockLot.Status.AVAILABLE, token="move-token"):
    return perform_stock_transfer(
        part=data["part"],
        from_location=data["source"],
        to_location=data["target"],
        quantity=quantity,
        stock_state=state,
        token=token,
        by=data["admin"],
    )


def login(client, data):
    assert client.login(username=data["admin"].username, password=PASSWORD)


def test_exact_article_703500875_finds_placed_stock(client, movement_data):
    make_lot(movement_data)
    login(client, movement_data)
    response = client.post(URL, {"action": "scan", "code": ARTICLE})
    assert response.status_code == 200
    assert response.context["object_kind"] == "stock_quantity"
    assert response.context["object"].part_exact_number == ARTICLE
    assert response.context["step"] == "scan_location"


@pytest.mark.parametrize("code", ["BAR-703500875", "  703500875  ", "703 500 875"])
def test_part_barcode_and_normalized_article_find_stock(client, movement_data, code):
    make_lot(movement_data)
    login(client, movement_data)
    response = client.post(URL, {"action": "scan", "code": code})
    assert response.context["object"].part_exact_number == ARTICLE


def test_serial_item_internal_barcode_is_accepted(client, movement_data):
    _part, item = make_serial_part(movement_data)
    login(client, movement_data)
    response = client.post(URL, {"action": "scan", "code": item.internal_barcode})
    assert response.context["object_kind"] == "part_item"
    assert response.context["object"].pk == item.pk


def test_single_serial_item_is_found_by_exact_article(client, movement_data):
    _part, item = make_serial_part(movement_data, number="SERIAL-EXACT")
    login(client, movement_data)
    response = client.post(URL, {"action": "scan", "code": " serial exact "})
    assert response.context["object_kind"] == "part_item"
    assert response.context["object"].pk == item.pk


def test_unplaced_serial_stock_has_clear_message(client, movement_data):
    _part, _item = make_serial_part(
        movement_data, number="SERIAL-UNPLACED", placed=False
    )
    login(client, movement_data)
    response = client.post(URL, {"action": "scan", "code": "SERIAL-UNPLACED"})
    assert "остаток не размещён" in response.context["error"]


def test_multiple_serial_items_require_item_choice(client, movement_data):
    part, _first = make_serial_part(movement_data, number="SERIAL-MULTI")
    line = BatchLine.objects.get(part_type=part)
    second = create_part_items(line, 1)[0]
    receive_part_item(second, to_location=movement_data["third"], by=movement_data["admin"])
    login(client, movement_data)
    response = client.post(URL, {"action": "scan", "code": "SERIAL-MULTI"})
    assert response.context["object"] is None
    assert len(response.context["candidates"]) == 2


def test_analog_lookup_is_not_an_operation_identity(client, movement_data):
    make_lot(movement_data)
    login(client, movement_data)
    response = client.post(URL, {"action": "scan", "code": "703500874"})
    assert response.context["object"] is None
    assert response.context["error"] == "Код не распознан."


def test_multiple_source_cells_require_choice(client, movement_data):
    make_lot(movement_data, movement_data["source"], suffix="one")
    make_lot(movement_data, movement_data["third"], suffix="two")
    login(client, movement_data)
    response = client.post(URL, {"action": "scan", "code": ARTICLE})
    assert response.context["object"] is None
    assert len(response.context["source_candidates"]) == 2
    assert "Выберите исходную ячейку" in response.context["error"]


def test_unknown_code_creates_no_stock_or_transfer(client, movement_data):
    login(client, movement_data)
    before = (StockLot.objects.count(), StockTransfer.objects.count())
    response = client.post(URL, {"action": "scan", "code": "UNKNOWN-MOVE"})
    assert response.context["error"] == "Код не распознан."
    assert (StockLot.objects.count(), StockTransfer.objects.count()) == before
    assert UnresolvedScan.objects.filter(raw_value="UNKNOWN-MOVE").exists()


def test_full_bulk_transfer_updates_primary_and_cache(movement_data):
    source_lot = make_lot(movement_data, quantity="5")
    document, created = transfer(movement_data, quantity="5")
    source_lot.refresh_from_db()
    target_lot = StockLot.objects.get(
        batch_line=source_lot.batch_line, location=movement_data["target"]
    )
    assert created is True
    assert document.quantity == Decimal("5")
    assert source_lot.quantity == 0
    assert source_lot.status == StockLot.Status.DEPLETED
    assert target_lot.quantity == 5
    assert not StockBalance.objects.filter(
        batch_line=source_lot.batch_line, location=movement_data["source"]
    ).exists()


def test_partial_bulk_transfer_keeps_source_remainder(movement_data):
    source_lot = make_lot(movement_data, quantity="10")
    transfer(movement_data, quantity="3")
    source_lot.refresh_from_db()
    target_lot = StockLot.objects.get(
        batch_line=source_lot.batch_line, location=movement_data["target"]
    )
    assert source_lot.quantity == 7
    assert target_lot.quantity == 3


def test_transfer_spans_multiple_procurement_lots_in_one_document(movement_data):
    first = make_lot(movement_data, quantity="2", suffix="one")
    second = make_lot(movement_data, quantity="4", suffix="two")
    document, _created = transfer(movement_data, quantity="5")
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.quantity == 0
    assert second.quantity == 1
    assert StockMovement.objects.filter(
        document_type="stock_transfer", document_id=document.pk
    ).count() == 2
    assert StockTransfer.objects.count() == 1


def test_transfer_merges_into_existing_target_lot(movement_data):
    source_lot = make_lot(movement_data, quantity="5")
    target = StockLot.objects.create(
        part_type=source_lot.part_type,
        batch=source_lot.batch,
        batch_line=source_lot.batch_line,
        location=movement_data["target"],
        quantity=Decimal("2"),
        initial_quantity=Decimal("2"),
        landed_unit_cost_rub=source_lot.landed_unit_cost_rub,
        status=StockLot.Status.AVAILABLE,
    )
    transfer(movement_data, quantity="3")
    target.refresh_from_db()
    assert target.quantity == 5
    assert StockLot.objects.filter(
        batch_line=source_lot.batch_line, location=movement_data["target"]
    ).count() == 1


@pytest.mark.parametrize("quantity", ["0", "-1", "bad"])
def test_non_positive_or_invalid_quantity_is_rejected(movement_data, quantity):
    lot = make_lot(movement_data, quantity="5")
    with pytest.raises(InventoryError):
        transfer(movement_data, quantity=quantity)
    lot.refresh_from_db()
    assert lot.quantity == 5
    assert StockTransfer.objects.count() == 0


def test_insufficient_quantity_rolls_back(movement_data):
    lot = make_lot(movement_data, quantity="5")
    with pytest.raises(InventoryError, match="Недостаточно"):
        transfer(movement_data, quantity="6")
    lot.refresh_from_db()
    assert lot.quantity == 5
    assert StockLot.objects.filter(location=movement_data["target"]).count() == 0


def test_reserved_quantity_is_not_movable(movement_data):
    lot = make_lot(movement_data, quantity="5")
    reservation = create_reservation(customer_name="Reserved", by=movement_data["admin"])
    add_stock_lot_to_reservation(reservation, lot, Decimal("4"), by=movement_data["admin"])
    activate_reservation(reservation, by=movement_data["admin"])
    sources, _items = movement_sources_for_part(movement_data["part"])
    assert sources[0].reserved == 4
    assert sources[0].movable == 1
    with pytest.raises(InventoryError, match="Недостаточно"):
        transfer(movement_data, quantity="2")
    lot.refresh_from_db()
    assert lot.quantity == 5


def test_quarantine_moves_only_as_quarantine(movement_data):
    source_lot = make_lot(
        movement_data, quantity="3", status=StockLot.Status.QUARANTINE
    )
    transfer(
        movement_data,
        quantity="2",
        state=StockLot.Status.QUARANTINE,
        token="quarantine-token",
    )
    source_lot.refresh_from_db()
    target = StockLot.objects.get(
        batch_line=source_lot.batch_line, location=movement_data["target"]
    )
    assert source_lot.quantity == 1
    assert target.quantity == 2
    assert target.status == StockLot.Status.QUARANTINE


def test_same_idempotency_token_does_not_move_twice(movement_data):
    lot = make_lot(movement_data, quantity="10")
    first, first_created = transfer(movement_data, quantity="3", token="same-token")
    second, second_created = transfer(movement_data, quantity="3", token="same-token")
    lot.refresh_from_db()
    assert first.pk == second.pk
    assert first_created is True
    assert second_created is False
    assert lot.quantity == 7
    assert StockMovement.objects.filter(
        document_type="stock_transfer", document_id=first.pk
    ).count() == 1


def test_repeated_confirm_post_after_full_move_is_idempotent(client, movement_data):
    lot = make_lot(movement_data, quantity="3")
    login(client, movement_data)
    payload = {
        "action": "confirm",
        "object_kind": "stock_quantity",
        "part_id": movement_data["part"].pk,
        "source_location_id": movement_data["source"].pk,
        "stock_state": StockLot.Status.AVAILABLE,
        "location_id": movement_data["target"].pk,
        "quantity": "3",
        "move_token": "double-post-token",
    }
    assert client.post(URL, payload).status_code == 302
    assert client.post(URL, payload).status_code == 302
    lot.refresh_from_db()
    assert lot.quantity == 0
    assert StockTransfer.objects.filter(token="double-post-token").count() == 1


def test_target_state_conflict_rolls_back_source(movement_data):
    source_lot = make_lot(movement_data, quantity="3")
    StockLot.objects.create(
        part_type=source_lot.part_type,
        batch=source_lot.batch,
        batch_line=source_lot.batch_line,
        location=movement_data["target"],
        quantity=Decimal("1"),
        initial_quantity=Decimal("1"),
        landed_unit_cost_rub=source_lot.landed_unit_cost_rub,
        status=StockLot.Status.QUARANTINE,
    )
    with pytest.raises(InventoryError, match="состояния нельзя смешивать"):
        transfer(movement_data, quantity="2", token="state-conflict")
    source_lot.refresh_from_db()
    assert source_lot.quantity == 3
    assert StockTransfer.objects.count() == 0


def test_reused_token_with_other_payload_is_rejected(movement_data):
    make_lot(movement_data, quantity="10")
    transfer(movement_data, quantity="3", token="reused-token")
    with pytest.raises(InventoryError, match="другой операции"):
        transfer(movement_data, quantity="2", token="reused-token")


def test_exception_mid_transfer_rolls_back_everything(movement_data):
    lot = make_lot(movement_data, quantity="5")
    with patch("apps.inventory.services._record_movement", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            transfer(movement_data, quantity="3", token="rollback-token")
    lot.refresh_from_db()
    assert lot.quantity == 5
    assert StockLot.objects.filter(location=movement_data["target"]).count() == 0
    assert StockTransfer.objects.count() == 0


def test_second_move_cannot_create_negative_stock(movement_data):
    lot = make_lot(movement_data, quantity="5")
    transfer(movement_data, quantity="4", token="first-token")
    with pytest.raises(InventoryError):
        transfer(movement_data, quantity="2", token="second-token")
    lot.refresh_from_db()
    assert lot.quantity == 1


def test_transfer_snapshot_keeps_exact_identity_and_both_cells(movement_data):
    make_lot(movement_data, quantity="5")
    document, _created = transfer(movement_data, quantity="2")
    assert document.part_number == ARTICLE
    assert document.part_name == "BRAKE PAD"
    assert document.manufacturer_name == "POLARIS"
    assert document.from_location_code == "S03-L03-D02-C04"
    assert document.to_location_code == "S03-L03-D02-C03"


def test_live_rows_and_warehouse_total_follow_transfer(movement_data):
    make_lot(movement_data, quantity="8")
    before = live_stock_rows(part_id=movement_data["part"].pk)
    assert sum(row.physical for row in before) == 8
    transfer(movement_data, quantity="3")
    after = live_stock_rows(part_id=movement_data["part"].pk)
    by_location = {row.location.pk: row for row in after}
    assert by_location[movement_data["source"].pk].physical == 5
    assert by_location[movement_data["target"].pk].physical == 3
    assert sum(row.physical for row in after) == 8


def test_balance_page_aggregates_same_part_and_cell_across_batches(client, movement_data):
    make_lot(movement_data, quantity="2", suffix="one")
    make_lot(movement_data, quantity="3", suffix="two")
    login(client, movement_data)
    response = client.get(reverse("balance_list"))
    rows = list(response.context["balances"])
    assert len(rows) == 1
    assert rows[0].part_exact_number == ARTICLE
    assert rows[0].physical == 5
    assert len(rows[0].batches) == 2


def test_live_stock_query_count_does_not_grow_with_lot_count(movement_data):
    make_lot(movement_data, quantity="2", suffix="one")
    with CaptureQueriesContext(connection) as one_lot:
        live_stock_rows(part_id=movement_data["part"].pk)
    make_lot(movement_data, quantity="3", suffix="two")
    with CaptureQueriesContext(connection) as two_lots:
        live_stock_rows(part_id=movement_data["part"].pk)
    assert len(two_lots) == len(one_lot)


def test_location_card_uses_live_stock_after_transfer(client, movement_data):
    make_lot(movement_data, quantity="5")
    login(client, movement_data)
    transfer(movement_data, quantity="5")
    source_html = client.get(
        reverse("location_detail", args=[movement_data["source"].pk])
    ).content.decode()
    target_html = client.get(
        reverse("location_detail", args=[movement_data["target"].pk])
    ).content.decode()
    assert "нет текущего физического остатка" in source_html
    assert ARTICLE in target_html


def test_initial_inventory_keeps_snapshot_but_current_block_is_live(client, movement_data):
    make_lot(movement_data, quantity="5")
    session = InventoryCountingSession.objects.create(
        storage_location=movement_data["source"],
        full_address=movement_data["source"].code,
        status=InventoryCountingSession.Status.POSTED,
        inventory_number="IC-TEST",
        posted_at=timezone.now(),
        created_by=movement_data["admin"],
    )
    InventoryCountingLine.objects.create(
        session=session,
        scanned_value=ARTICLE,
        normalized_value=ARTICLE,
        warehouse_part=movement_data["part"],
        display_name=movement_data["part"].name,
        source=InventoryCountingLine.Source.WAREHOUSE,
        quantity_counted=Decimal("5"),
        scan_count=5,
        final_customer_price_rub=Decimal("100"),
    )
    transfer(movement_data, quantity="5")
    login(client, movement_data)
    response = client.get(reverse("initial_inventory_detail", args=[session.pk]))
    assert response.context["breakdown"]["total_quantity"] == 5
    assert response.context["live_stock_rows"] == []
    html = response.content.decode()
    assert "Снимок на момент первичного ввода" in html
    assert "Сейчас в этой ячейке нет физического остатка" in html


def test_diagnostics_is_clean_after_transfer(capsys, movement_data):
    make_lot(movement_data, quantity="5")
    transfer(movement_data, quantity="2")
    call_command("debug_stock_location_consistency")
    assert "ИТОГ: расхождений 0" in capsys.readouterr().out


def test_diagnostics_detects_stale_balance(capsys, movement_data):
    lot = make_lot(movement_data, quantity="5")
    balance = StockBalance.objects.get(batch_line=lot.batch_line, location=lot.location)
    balance.quantity_physical = Decimal("99")
    balance.save(update_fields=["quantity_physical"])
    call_command("debug_stock_location_consistency")
    output = capsys.readouterr().out
    assert "stock balance" in output
    assert "ИТОГ: расхождений 1" in output


def test_reverse_for_movement_page_is_stable():
    assert reverse("scanner_move") == "/scanner/move/"
