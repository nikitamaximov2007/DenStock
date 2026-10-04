"""Reliability Phase 2: expected conflicts, duplicate submits and stale state.

Runs on SQLite and PostgreSQL. Every scenario reads the database fresh after
the request and asserts the stock, the history and the documents, not only
the HTTP status.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.actions.cart import (
    add_scan,
    clear_cart,
    complete_cart,
    discard_cart,
    open_cart,
    remove_row,
    set_row_quantity,
)
from apps.actions.models import WarehouseAction
from apps.actions.services import ActionError, perform_action
from apps.inventory.models import StockMovement
from apps.receipts.models import Receipt, ReceiptLine
from apps.receipts.services import (
    ReceiptError,
    add_line,
    create_receipt,
    post_receipt,
    remove_line,
    update_line,
    update_receipt,
)
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    create_repair_order,
)
from apps.returns.models import StockReturnLine
from apps.returns.services import add_sale_line_return, create_return
from apps.sales.models import Reservation, Sale, SaleLine
from apps.sales.services import (
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    create_reservation,
    create_sale,
)
from apps.stocktaking.section_recount import create_cell_recount
from apps.writeoffs.models import WriteOffDocument
from apps.writeoffs.services import (
    add_stock_lot_to_write_off,
    create_write_off,
)
from tests.reliability_support import World, lot_qty, movement_count

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    return World()


@pytest.fixture
def boss(client, world):
    client.force_login(world.admin)
    return client


def _messages(response):
    return [str(m) for m in response.wsgi_request._messages]


# --- Expected conflicts stay controlled (no HTTP 500) --------------------------------------


def _lock_cell(world, location):
    return create_cell_recount(location=location, by=world.admin)


def test_sale_completion_in_a_cell_under_recount_is_refused_not_a_server_error(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    sale = create_sale(customer_name="Клиент", by=world.admin)
    add_stock_lot_to_sale(sale, lot, Decimal("2"), unit_price=Decimal("100"))
    _lock_cell(world, world.loc_a)

    response = boss.post(reverse("sale_complete", args=[sale.pk]))

    assert response.status_code == 302
    assert any("заблокирована" in text for text in _messages(response))
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.DRAFT
    assert lot_qty(lot) == Decimal("5")
    assert movement_count(document_type="sale") == 0


def test_repair_completion_in_a_cell_under_recount_is_refused_not_a_server_error(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    order = create_repair_order(customer_name="Клиент", by=world.admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("2"))
    _lock_cell(world, world.loc_a)

    response = boss.post(reverse("repair_order_complete", args=[order.pk]))

    assert response.status_code == 302
    assert RepairOrder.objects.get(pk=order.pk).status == RepairOrder.Status.DRAFT
    assert lot_qty(lot) == Decimal("5")


def test_write_off_completion_in_a_cell_under_recount_is_refused_not_a_server_error(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=world.admin)
    add_stock_lot_to_write_off(doc, lot, Decimal("1"))
    _lock_cell(world, world.loc_a)

    response = boss.post(reverse("write_off_complete", args=[doc.pk]))

    assert response.status_code == 302
    assert WriteOffDocument.objects.get(pk=doc.pk).status == WriteOffDocument.Status.DRAFT
    assert lot_qty(lot) == Decimal("5")


def test_reservation_activation_in_a_cell_under_recount_is_refused_not_a_server_error(
    world, boss
):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    reservation = create_reservation(customer_name="Клиент", by=world.admin)
    add_stock_lot_to_reservation(reservation, lot, Decimal("1"))
    _lock_cell(world, world.loc_a)

    response = boss.post(reverse("reservation_activate", args=[reservation.pk]))

    assert response.status_code == 302
    assert Reservation.objects.get(pk=reservation.pk).status == Reservation.Status.DRAFT


def test_quick_action_sale_in_a_cell_under_recount_is_refused_not_a_server_error(world):
    world.make_lot(world.part_x, world.loc_a, 5)
    _lock_cell(world, world.loc_a)

    with pytest.raises(ActionError, match="заблокирована"):
        perform_action(
            part=world.part_x, location=world.loc_a, action_type="sale", quantity="1",
            customer_comment="Клиент", by=world.admin, request_token="locked-cell-1",
        )
    assert movement_count(document_type="sale") == 0
    assert not WarehouseAction.objects.exists()
    assert not Sale.objects.exists()


def test_cancelling_a_quick_sale_with_an_open_return_draft_is_refused_not_a_server_error(
    world, boss
):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    action = perform_action(
        part=world.part_x, location=world.loc_a, action_type="sale", quantity="2",
        customer_comment="Клиент", by=world.admin, request_token="qa-draft-return",
    )
    sale = Sale.objects.get(pk=action.sale_id)
    ret = create_return(source=sale, reason="Клиент принёс")
    add_sale_line_return(
        ret, SaleLine.objects.get(sale=sale), Decimal("1"), to_location=world.loc_a,
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
    )

    response = boss.post(reverse("actions_cancel", args=[action.pk]), {"reason": "Ошибка"})

    assert response.status_code == 302
    assert any("черновик возврата" in text for text in _messages(response))
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    assert WarehouseAction.objects.get(pk=action.pk).status == WarehouseAction.Status.ACTIVE
    assert lot_qty(lot) == Decimal("3")


# --- Duplicate submit: one intention, one effect ------------------------------------------


def _quick_write_off_post(world, lot, token, quantity="1"):
    return {
        "q": "RX-100",
        "part_id": str(world.part_x.pk),
        "reason": "Брак",
        "business_author": "Денис",
        "quantity": quantity,
        "location_id": str(lot.location_id),
        "request_token": token,
    }


def test_quick_write_off_form_carries_a_one_time_token(world, boss):
    world.make_lot(world.part_x, world.loc_a, 5)
    response = boss.get(reverse("write_off_quick"), {"q": "RX-100"})
    assert response.status_code == 200
    assert 'name="request_token"' in response.content.decode()
    assert "data-idempotent-form" in response.content.decode()


def test_quick_write_off_double_submit_writes_off_once(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    data = _quick_write_off_post(world, lot, "wo-token-1")

    first = boss.post(reverse("write_off_quick"), data)
    second = boss.post(reverse("write_off_quick"), data)

    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"]
    assert WriteOffDocument.objects.count() == 1
    assert lot_qty(lot) == Decimal("4")
    assert movement_count(document_type="write_off") == 1


def test_quick_write_off_token_reused_for_another_intention_is_refused(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    boss.post(reverse("write_off_quick"), _quick_write_off_post(world, lot, "wo-token-2"))
    response = boss.post(
        reverse("write_off_quick"), _quick_write_off_post(world, lot, "wo-token-2", quantity="2")
    )
    assert response.status_code == 200
    assert WriteOffDocument.objects.count() == 1
    assert lot_qty(lot) == Decimal("4")


def test_quick_write_offs_with_different_tokens_are_two_intentions(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    boss.post(reverse("write_off_quick"), _quick_write_off_post(world, lot, "wo-a"))
    boss.post(reverse("write_off_quick"), _quick_write_off_post(world, lot, "wo-b"))
    assert WriteOffDocument.objects.count() == 2
    assert lot_qty(lot) == Decimal("3")


# --- Stale state: the server re-reads the document, never trusts the caller's copy ----------


def _posted_receipt(world):
    receipt = create_receipt(supplier=world.supplier, by=world.admin)
    line = add_line(
        receipt, part_type=world.part_x, quantity="3", unit_cost_rub=Decimal("50"),
        location=world.loc_a,
    )
    stale_receipt = Receipt.objects.get(pk=receipt.pk)
    stale_line = ReceiptLine.objects.select_related("receipt").get(pk=line.pk)
    post_receipt(Receipt.objects.get(pk=receipt.pk), by=world.admin)
    return receipt, stale_receipt, stale_line


def _receipt_state(receipt):
    return list(
        ReceiptLine.objects.filter(receipt=receipt).order_by("pk").values_list(
            "pk", "part_type_id", "quantity", "unit_cost_rub", "location_id", "batch_line_id"
        )
    )


@pytest.mark.parametrize("edit", ["add", "update", "remove", "header"])
def test_a_page_opened_before_posting_cannot_change_the_posted_receipt(world, edit):
    receipt, stale_receipt, stale_line = _posted_receipt(world)
    before = _receipt_state(receipt)
    header = Receipt.objects.filter(pk=receipt.pk).values().get()

    with pytest.raises(ReceiptError):
        if edit == "add":
            add_line(stale_receipt, part_type=world.part_x, quantity="7",
                     unit_cost_rub=Decimal("50"), location=world.loc_a)
        elif edit == "update":
            update_line(stale_line, part_type=world.part_x, quantity="9",
                        unit_cost_rub=Decimal("50"), location=world.loc_a)
        elif edit == "remove":
            remove_line(stale_line)
        else:
            update_receipt(stale_receipt, supplier=None, received_at=stale_receipt.received_at,
                           comment="Правка задним числом")

    assert _receipt_state(receipt) == before
    assert Receipt.objects.filter(pk=receipt.pk).values().get() == header


def _completed_cart(world, lot):
    cart = open_cart("sale", by=world.admin)
    add_scan(cart, world.part_x, world.loc_a, quantity=Decimal("2"), by=world.admin)
    stale = Sale.objects.get(pk=cart.pk)
    complete_cart(Sale.objects.get(pk=cart.pk), customer_comment="Клиент", by=world.admin)
    return stale


@pytest.mark.parametrize("edit", ["remove", "clear", "set_zero", "discard", "add"])
def test_a_cart_page_opened_before_completion_cannot_change_the_completed_sale(world, edit):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    stale = _completed_cart(world, lot)
    before = list(SaleLine.objects.filter(sale_id=stale.pk).values_list("pk", "quantity"))

    with pytest.raises(ActionError):
        if edit == "remove":
            remove_row(stale, world.part_x, world.loc_a, by=world.admin)
        elif edit == "clear":
            clear_cart(stale, by=world.admin)
        elif edit == "set_zero":
            set_row_quantity(stale, world.part_x, world.loc_a, 0, by=world.admin)
        elif edit == "discard":
            discard_cart(stale, by=world.admin)
        else:
            add_scan(stale, world.part_x, world.loc_a, quantity=Decimal("1"), by=world.admin)

    sale = Sale.objects.get(pk=stale.pk)
    assert sale.status == Sale.Status.COMPLETED
    assert list(SaleLine.objects.filter(sale=sale).values_list("pk", "quantity")) == before
    assert lot_qty(lot) == Decimal("3")
    assert StockMovement.objects.filter(document_type="sale", document_id=sale.pk).count() == 1


# --- Partial line cancellation: one confirmation, one cancellation -----------------------


def _completed_sale_line(world, lot, qty="3"):
    sale = create_sale(customer_name="Клиент", by=world.admin)
    add_stock_lot_to_sale(sale, lot, Decimal(qty), unit_price=Decimal("100"))
    from apps.sales.services import complete_sale

    complete_sale(sale, by=world.admin)
    return SaleLine.objects.get(sale=sale)


def _cancel_form(response_or_seen, quantity="1"):
    return {
        "quantity": quantity, "reason": "Ошибка кассира", "author": "Денис",
        "remaining_seen": response_or_seen,
    }


def test_sale_line_cancellation_confirmation_carries_what_it_showed(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    line = _completed_sale_line(world, lot)
    page = boss.get(reverse("sale_line_cancel", args=[line.pk]))
    assert 'name="remaining_seen" value="3"' in page.content.decode()


def test_sale_line_cancellation_double_submit_cancels_once(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    line = _completed_sale_line(world, lot)
    url = reverse("sale_line_cancel", args=[line.pk])

    first = boss.post(url, _cancel_form("3"))
    second = boss.post(url, _cancel_form("3"))

    assert first.status_code == 302
    assert second.status_code == 200
    assert any("изменилась" in text for text in _messages(second))
    assert lot_qty(lot) == Decimal("3")
    assert StockMovement.objects.filter(movement_type="return_lot").count() == 1
    # The refusal shows the new remaining quantity for a deliberate retry.
    assert 'name="remaining_seen" value="2"' in second.content.decode()


def test_repair_line_cancellation_double_submit_cancels_once(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    order = create_repair_order(customer_name="Клиент", by=world.admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("3"))
    from apps.repairs.services import complete_repair_order

    complete_repair_order(order, by=world.admin)
    line = order.lines.get()
    url = reverse("repair_line_cancel", args=[line.pk])

    first = boss.post(url, _cancel_form("3"))
    second = boss.post(url, _cancel_form("3"))

    assert first.status_code == 302
    assert second.status_code == 200
    assert lot_qty(lot) == Decimal("3")
    assert StockMovement.objects.filter(movement_type="return_lot").count() == 1


def test_a_malformed_remaining_value_is_a_controlled_refusal(world, boss):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    line = _completed_sale_line(world, lot)
    response = boss.post(reverse("sale_line_cancel", args=[line.pk]), _cancel_form("три"))
    assert response.status_code == 200
    assert lot_qty(lot) == Decimal("2")
