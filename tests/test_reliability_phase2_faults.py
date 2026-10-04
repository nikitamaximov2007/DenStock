"""Reliability Phase 2: fault injection into every critical stock operation.

A test-only fault is raised at the SQL level (``connection.execute_wrapper``)
at a meaningful stage of the operation: right after the document or line is
written, after the authoritative stock row is read, after the stock row is
changed, after the movement or the balance cache or the WarehouseAction is
written, or just before the final status update. No production switch exists.

For every (operation, stage) pair the test proves, from fresh queries over
every business table:

1. the failed call left NO committed business effect (a full table snapshot
   is identical to the one taken before);
2. a retry succeeds and its effect is exactly the operation's effect;
3. a repeat of the successful call adds nothing.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pytest
from django.db import connection

from apps.actions.cart import add_scan, complete_cart, open_cart
from apps.actions.models import WarehouseAction
from apps.actions.services import ActionError, perform_action
from apps.catalog.models import Manufacturer
from apps.customer_requests.models import CustomerRequest
from apps.customer_requests.sale_conversion import (
    CustomerRequestSaleError,
    complete_request_sale,
    prepare_request_sale,
)
from apps.customer_requests.services import (
    RequestLineInput,
    change_request_status,
    create_customer_request,
)
from apps.inventory.models import (
    NumberSequence,
    PartPreferredLocation,
    StockBalance,
    StockLot,
    StockMovement,
    StockTransfer,
)
from apps.inventory.services import InventoryError, perform_stock_transfer
from apps.procurement.models import Batch, BatchLine
from apps.receipts.models import Receipt, ReceiptLine
from apps.receipts.services import ReceiptError, add_line, create_receipt, post_receipt
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.repairs.services import (
    RepairError,
    add_stock_lot_to_repair_order,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.models import StockReturn, StockReturnLine
from apps.returns.services import (
    ReturnError,
    add_repair_line_return,
    add_sale_line_return,
    cancel_return,
    complete_return,
    create_return,
)
from apps.sales.models import Reservation, ReservationLine, Sale, SaleLine
from apps.sales.services import (
    ReservationError,
    SaleError,
    activate_reservation,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    cancel_reservation,
    cancel_sale,
    cancel_sale_line_quantity,
    complete_sale,
    create_reservation,
    create_sale,
    create_sale_from_reservation,
    remove_reservation_line,
)
from apps.stocktaking.models import InventoryCountDocument, InventoryCountLine
from apps.stocktaking.services import (
    StocktakingError,
    add_stock_lot_count_line,
    complete_inventory_count,
    create_inventory_count,
    update_counted_quantity,
)
from apps.writeoffs.models import WriteOffDocument, WriteOffLine
from apps.writeoffs.services import (
    WriteOffError,
    add_stock_lot_to_write_off,
    cancel_write_off,
    complete_write_off,
    create_write_off,
    quick_write_off,
)
from tests.reliability_support import World, lot_qty

pytestmark = pytest.mark.django_db

BUSINESS_MODELS = [
    StockLot, StockMovement, StockBalance, StockTransfer, PartPreferredLocation,
    WarehouseAction, Sale, SaleLine, Reservation, ReservationLine, RepairOrder,
    RepairIssueLine, StockReturn, StockReturnLine, WriteOffDocument, WriteOffLine,
    Receipt, ReceiptLine, Batch, BatchLine, InventoryCountDocument, InventoryCountLine,
    CustomerRequest, NumberSequence,
]
DOMAIN_ERRORS = (
    SaleError, RepairError, ReservationError, ReturnError, WriteOffError, ReceiptError,
    StocktakingError, ActionError, InventoryError, CustomerRequestSaleError,
)


class InjectedFault(Exception):
    """The test-only failure; never a domain error, never caught by services."""


def snapshot():
    return {
        model.__name__: list(model.objects.order_by("pk").values()) for model in BUSINESS_MODELS
    }


@contextmanager
def fault_at(matches, *, before=False):
    """Raise InjectedFault at the first statement matching ``matches``."""
    hits = {"n": 0}

    def wrapper(execute, sql, params, many, context):
        hit = matches(sql) and hits["n"] == 0
        if hit and before:
            hits["n"] += 1
            raise InjectedFault(sql[:80])
        result = execute(sql, params, many, context)
        if hit:
            hits["n"] += 1
            raise InjectedFault(sql[:80])
        return result

    with connection.execute_wrapper(wrapper):
        yield hits


def _insert(table):
    return lambda sql: sql.lstrip().startswith(f'INSERT INTO "{table}"')


def _update(table, column=None):
    def matches(sql):
        sql = sql.lstrip()
        return sql.startswith(f'UPDATE "{table}"') and (column is None or f'"{column}"' in sql)

    return matches


def _write(table):
    def matches(sql):
        sql = sql.lstrip()
        return any(
            sql.startswith(f'{verb} "{table}"') for verb in ("INSERT INTO", "UPDATE", "DELETE FROM")
        )

    return matches


def _read(table):
    return lambda sql: sql.lstrip().startswith("SELECT") and f'FROM "{table}"' in sql


# Stage -> (predicate, raise before the statement?)
STAGES = {
    "after_stock_read": (_read("inventory_stocklot"), False),
    "after_stock_change": (_update("inventory_stocklot"), False),
    "after_stock_insert": (_insert("inventory_stocklot"), False),
    "after_movement": (_insert("inventory_stockmovement"), False),
    "after_balance": (_write("inventory_stockbalance"), False),
    "after_action": (_insert("actions_warehouseaction"), False),
    "after_item_change": (_update("inventory_partitem"), False),
    "after_item_insert": (_insert("inventory_partitem"), False),
}


def _status_stage(table):
    return (_update(table, "status"), True, f"before_status:{table}")


def _header_stage(table):
    return (_insert(table), False, f"after_insert:{table}")


# --- Operations ---------------------------------------------------------------------------
#
# Each scenario returns (operation, effect_check). ``operation`` must be
# re-runnable: it re-reads its document. ``effect_check`` runs after the
# successful retry and asserts the operation's exact effect.


def _sale_draft(w, lot, qty="2"):
    sale = create_sale(customer_name="Клиент", by=w.admin)
    add_stock_lot_to_sale(sale, lot, Decimal(qty), unit_price=Decimal("100"), by=w.admin)
    return sale


def scenario_manual_sale(w):
    lot_x = w.make_lot(w.part_x, w.loc_a, 5)
    lot_y = w.make_lot(w.part_y, w.loc_b, 5)
    sale = _sale_draft(w, lot_x)
    add_stock_lot_to_sale(sale, lot_y, Decimal("1"), unit_price=Decimal("100"), by=w.admin)

    def check():
        assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
        assert (lot_qty(lot_x), lot_qty(lot_y)) == (Decimal("3"), Decimal("4"))
        assert StockMovement.objects.filter(document_type="sale", document_id=sale.pk).count() == 2

    return (lambda: complete_sale(Sale.objects.get(pk=sale.pk), by=w.admin)), check


def scenario_quick_action_sale(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)

    def op():
        return perform_action(
            part=w.part_x, location=w.loc_a, action_type="sale", quantity="2",
            customer_comment="Клиент", by=w.admin, request_token="fault-qa-sale",
        )

    def check():
        action = WarehouseAction.objects.get()
        assert action.sale.status == Sale.Status.COMPLETED
        assert lot_qty(lot) == Decimal("3")
        assert StockMovement.objects.filter(document_type="sale").count() == 1

    return op, check


def scenario_quick_action_repair(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)

    def op():
        return perform_action(
            part=w.part_x, location=w.loc_a, action_type="repair", quantity="2",
            customer_comment="Клиент", by=w.admin, request_token="fault-qa-repair",
        )

    def check():
        action = WarehouseAction.objects.get()
        assert action.repair_order.status == RepairOrder.Status.COMPLETED
        assert lot_qty(lot) == Decimal("3")

    return op, check


def scenario_cart_sale(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    cart = open_cart("sale", by=w.admin)
    add_scan(cart, w.part_x, w.loc_a, quantity=Decimal("2"), by=w.admin)

    def op():
        return complete_cart(
            Sale.objects.get(pk=cart.pk), customer_comment="Клиент", by=w.admin,
            request_token="fault-cart",
        )

    def check():
        assert Sale.objects.get(pk=cart.pk).status == Sale.Status.COMPLETED
        assert WarehouseAction.objects.filter(sale_id=cart.pk).count() == 1
        assert lot_qty(lot) == Decimal("3")

    return op, check


def scenario_reservation_to_sale(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    reservation = create_reservation(customer_name="Клиент", by=w.admin)
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=w.admin)
    activate_reservation(reservation, by=w.admin)
    sale = create_sale_from_reservation(reservation, by=w.admin)

    def check():
        assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
        assert Reservation.objects.get(pk=reservation.pk).status == Reservation.Status.CONVERTED
        assert lot_qty(lot) == Decimal("3")
        balance = StockBalance.objects.get(batch_line_id=lot.batch_line_id, location=w.loc_a)
        assert balance.quantity_reserved == Decimal("0")
        assert balance.quantity_available == Decimal("3")

    return (lambda: complete_sale(Sale.objects.get(pk=sale.pk), by=w.admin)), check


def scenario_customer_request_sale(w):
    from tests.test_customer_requests import POLICY

    part = w.make_part("REQ-FAULT", "Деталь заявки")
    part.manufacturer = Manufacturer.objects.create(name="BRP fault")
    part.is_public = True
    part.save(update_fields=["manufacturer", "is_public"])
    lot = w.make_lot(part, w.loc_a, 3)
    request, _ = create_customer_request(
        customer_name="Александр Пушкарев", customer_phone="89090000001",
        preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
        lines=[RequestLineInput(part_id=part.pk, quantity="1", supply_inquiry=False)],
        privacy_policy_version=POLICY, personal_data_consent_version=POLICY,
        submission_key="customer-request-fault-key",
    )
    change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.IN_PROGRESS, by=w.admin
    )
    sale = prepare_request_sale(request_id=request.pk, by=w.admin, create_customer=True)

    def check():
        assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
        assert lot_qty(lot) == Decimal("2")
        assert StockMovement.objects.filter(document_type="sale", document_id=sale.pk).count() == 1

    return (
        lambda: complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=w.admin)
    ), check


def scenario_repair(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    order = create_repair_order(customer_name="Клиент", by=w.admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("2"), by=w.admin)

    def check():
        assert RepairOrder.objects.get(pk=order.pk).status == RepairOrder.Status.COMPLETED
        assert lot_qty(lot) == Decimal("3")
        assert RepairIssueLine.objects.get(repair_order=order).issued_at is not None

    return (lambda: complete_repair_order(RepairOrder.objects.get(pk=order.pk), by=w.admin)), check


def scenario_reservation_activate(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    reservation = create_reservation(customer_name="Клиент", by=w.admin)
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=w.admin)

    def check():
        assert Reservation.objects.get(pk=reservation.pk).status == Reservation.Status.ACTIVE
        balance = StockBalance.objects.get(batch_line_id=lot.batch_line_id, location=w.loc_a)
        assert (balance.quantity_physical, balance.quantity_reserved) == (
            Decimal("5"), Decimal("2"),
        )
        assert lot_qty(lot) == Decimal("5")

    return (
        lambda: activate_reservation(Reservation.objects.get(pk=reservation.pk), by=w.admin)
    ), check


def _active_reservation(w, lot):
    reservation = create_reservation(customer_name="Клиент", by=w.admin)
    line = add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=w.admin)
    activate_reservation(reservation, by=w.admin)
    return reservation, line


def scenario_reservation_cancel(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    reservation, _line = _active_reservation(w, lot)

    def check():
        assert Reservation.objects.get(pk=reservation.pk).status == Reservation.Status.CANCELED
        balance = StockBalance.objects.get(batch_line_id=lot.batch_line_id, location=w.loc_a)
        assert balance.quantity_reserved == Decimal("0")

    return (
        lambda: cancel_reservation(Reservation.objects.get(pk=reservation.pk), by=w.admin)
    ), check


def scenario_reservation_line_remove(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    reservation, line = _active_reservation(w, lot)

    def check():
        assert not ReservationLine.objects.filter(pk=line.pk).exists()
        balance = StockBalance.objects.get(batch_line_id=lot.batch_line_id, location=w.loc_a)
        assert balance.quantity_reserved == Decimal("0")

    return (lambda: remove_reservation_line(line, by=w.admin)), check


def _completed_sale(w, lot, qty="3"):
    sale = _sale_draft(w, lot, qty)
    return complete_sale(sale, by=w.admin)


def scenario_sale_cancel(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    sale = _completed_sale(w, lot)
    line = SaleLine.objects.get(sale=sale)
    snapshot_line = SaleLine.objects.filter(pk=line.pk).values().get()

    def check():
        assert Sale.objects.get(pk=sale.pk).status == Sale.Status.CANCELED
        assert lot_qty(lot) == Decimal("5")
        assert StockMovement.objects.filter(
            document_type="sale", document_id=sale.pk, movement_type="return_lot"
        ).count() == 1
        assert SaleLine.objects.filter(pk=line.pk).values().get() == snapshot_line  # HISTORY-1

    return (
        lambda: cancel_sale(
            Sale.objects.get(pk=sale.pk), by=w.admin, reason="Ошибка", author="Денис"
        )
    ), check


def scenario_sale_line_cancel(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    sale = _completed_sale(w, lot)
    line = SaleLine.objects.get(sale=sale)

    def check():
        assert lot_qty(lot) == Decimal("3")
        assert StockReturn.objects.get().status == StockReturn.Status.COMPLETED
        assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED

    # The confirmation page showed 3 remaining: a repeat of that page is refused.
    return (
        lambda: cancel_sale_line_quantity(
            SaleLine.objects.get(pk=line.pk), "1", reason="Ошибка", author="Денис", by=w.admin,
            expected_remaining="3",
        )
    ), check


def scenario_repair_cancel(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    order = create_repair_order(customer_name="Клиент", by=w.admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("2"), by=w.admin)
    complete_repair_order(order, by=w.admin)

    def check():
        assert RepairOrder.objects.get(pk=order.pk).status == RepairOrder.Status.CANCELED
        assert lot_qty(lot) == Decimal("5")

    return (
        lambda: cancel_repair_order(
            RepairOrder.objects.get(pk=order.pk), by=w.admin, reason="Ошибка", author="Денис"
        )
    ), check


def scenario_sale_return(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    sale = _completed_sale(w, lot)
    ret = create_return(source=sale, reason="Вернули")
    add_sale_line_return(
        ret, SaleLine.objects.get(sale=sale), Decimal("2"), to_location=w.loc_a,
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
    )

    def check():
        assert StockReturn.objects.get(pk=ret.pk).status == StockReturn.Status.COMPLETED
        assert lot_qty(lot) == Decimal("4")

    return (lambda: complete_return(StockReturn.objects.get(pk=ret.pk), by=w.admin)), check


def scenario_repair_return(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    order = create_repair_order(customer_name="Клиент", by=w.admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("2"), by=w.admin)
    complete_repair_order(order, by=w.admin)
    ret = create_return(source=RepairOrder.objects.get(pk=order.pk), reason="Вернули")
    add_repair_line_return(
        ret, RepairIssueLine.objects.get(repair_order=order), Decimal("1"),
        to_location=w.loc_a, restock_status=StockReturnLine.RestockStatus.AVAILABLE,
    )

    def check():
        assert StockReturn.objects.get(pk=ret.pk).status == StockReturn.Status.COMPLETED
        assert lot_qty(lot) == Decimal("4")
        assert WarehouseAction.objects.filter(
            stock_return=ret, action_type=WarehouseAction.Type.REPAIR_RETURN
        ).count() == 1

    return (lambda: complete_return(StockReturn.objects.get(pk=ret.pk), by=w.admin)), check


def scenario_return_cancel(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    sale = _completed_sale(w, lot)
    ret = create_return(source=sale, reason="Вернули")
    add_sale_line_return(
        ret, SaleLine.objects.get(sale=sale), Decimal("2"), to_location=w.loc_a,
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
    )
    complete_return(ret, by=w.admin)

    def check():
        assert StockReturn.objects.get(pk=ret.pk).status == StockReturn.Status.CANCELED
        assert lot_qty(lot) == Decimal("2")

    return (
        lambda: cancel_return(StockReturn.objects.get(pk=ret.pk), by=w.admin, reason="Ошибка")
    ), check


def scenario_write_off(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=w.admin)
    add_stock_lot_to_write_off(doc, lot, Decimal("2"))

    def check():
        assert WriteOffDocument.objects.get(pk=doc.pk).status == WriteOffDocument.Status.COMPLETED
        assert lot_qty(lot) == Decimal("3")

    return (
        lambda: complete_write_off(WriteOffDocument.objects.get(pk=doc.pk), by=w.admin)
    ), check


def scenario_write_off_cancel(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=w.admin)
    add_stock_lot_to_write_off(doc, lot, Decimal("2"))
    complete_write_off(doc, by=w.admin)

    def check():
        assert WriteOffDocument.objects.get(pk=doc.pk).status == WriteOffDocument.Status.CANCELED
        assert lot_qty(lot) == Decimal("5")

    return (
        lambda: cancel_write_off(WriteOffDocument.objects.get(pk=doc.pk), by=w.admin)
    ), check


def scenario_quick_write_off(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)

    def op():
        return quick_write_off(
            part=w.part_x, scanned_code="RX-100", reason="Брак", business_author="Денис",
            quantity="2", location_id=w.loc_a.pk, by=w.admin, request_token="fault-quick-wo",
        )

    def check():
        assert WriteOffDocument.objects.get().status == WriteOffDocument.Status.COMPLETED
        assert lot_qty(lot) == Decimal("3")

    return op, check


def scenario_transfer(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)

    def op():
        return perform_stock_transfer(
            part=w.part_x, from_location=w.loc_a, to_location=w.loc_b, quantity="2",
            stock_state=StockLot.Status.AVAILABLE, token="fault-move", by=w.admin,
        )

    def check():
        assert StockTransfer.objects.count() == 1
        assert lot_qty(lot) == Decimal("3")
        moved = StockLot.objects.get(part_type=w.part_x, location=w.loc_b)
        assert moved.quantity == Decimal("2")
        assert StockMovement.objects.filter(document_type="stock_transfer").count() == 1

    return op, check


def scenario_receipt(w):
    receipt = create_receipt(supplier=w.supplier, by=w.admin)
    add_line(receipt, part_type=w.part_x, quantity="4", unit_cost_rub=Decimal("50"),
             location=w.loc_c)

    def check():
        assert Receipt.objects.get(pk=receipt.pk).status == Receipt.Status.POSTED
        received = StockLot.objects.get(part_type=w.part_x, location=w.loc_c)
        assert received.quantity == Decimal("4")
        assert StockMovement.objects.filter(stock_lot=received).count() == 1

    return (lambda: post_receipt(Receipt.objects.get(pk=receipt.pk), by=w.admin)), check


def scenario_inventory_count(w):
    lot = w.make_lot(w.part_x, w.loc_a, 5)
    doc = create_inventory_count(scope_location=w.loc_a, by=w.admin)
    line = add_stock_lot_count_line(doc, lot, by=w.admin)
    update_counted_quantity(line, Decimal("3"), by=w.admin)

    def check():
        assert InventoryCountDocument.objects.get(pk=doc.pk).status == (
            InventoryCountDocument.Status.COMPLETED
        )
        assert lot_qty(lot) == Decimal("3")
        assert StockMovement.objects.filter(document_type="inventory_count").count() == 1

    return (
        lambda: complete_inventory_count(
            InventoryCountDocument.objects.get(pk=doc.pk), by=w.admin
        )
    ), check


def _item_statuses(items):
    from apps.inventory.models import PartItem

    return [PartItem.objects.get(pk=item.pk).status for item in items]


def scenario_serial_sale(w):
    from apps.inventory.models import PartItem
    from apps.sales.services import add_part_item_to_sale

    part = w.make_serial_part("SN-300", "Блок серийный")
    items = w.make_items(part, w.loc_a, 2)
    sale = create_sale(customer_name="Клиент", by=w.admin)
    for item in items:
        add_part_item_to_sale(sale, item, unit_price=Decimal("500"), by=w.admin)

    def check():
        assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
        assert _item_statuses(items) == [PartItem.Status.SOLD] * 2
        assert StockMovement.objects.filter(document_type="sale", document_id=sale.pk).count() == 2

    return (lambda: complete_sale(Sale.objects.get(pk=sale.pk), by=w.admin)), check


def scenario_serial_sale_cancel(w):
    from apps.inventory.models import PartItem
    from apps.sales.services import add_part_item_to_sale

    part = w.make_serial_part("SN-301", "Блок серийный")
    items = w.make_items(part, w.loc_a, 2)
    sale = create_sale(customer_name="Клиент", by=w.admin)
    for item in items:
        add_part_item_to_sale(sale, item, unit_price=Decimal("500"), by=w.admin)
    complete_sale(sale, by=w.admin)

    def check():
        assert Sale.objects.get(pk=sale.pk).status == Sale.Status.CANCELED
        assert _item_statuses(items) == [PartItem.Status.AVAILABLE] * 2

    return (
        lambda: cancel_sale(
            Sale.objects.get(pk=sale.pk), by=w.admin, reason="Ошибка", author="Денис"
        )
    ), check


def scenario_serial_receipt(w):
    from apps.inventory.models import PartItem

    part = w.make_serial_part("SN-302", "Блок серийный")
    receipt = create_receipt(supplier=w.supplier, by=w.admin)
    add_line(receipt, part_type=part, quantity="2", unit_cost_rub=Decimal("50"),
             location=w.loc_c)

    def check():
        assert Receipt.objects.get(pk=receipt.pk).status == Receipt.Status.POSTED
        items = PartItem.objects.filter(part_type=part)
        assert items.count() == 2
        assert {item.status for item in items} == {PartItem.Status.AVAILABLE}
        assert {item.current_location_id for item in items} == {w.loc_c.pk}

    return (lambda: post_receipt(Receipt.objects.get(pk=receipt.pk), by=w.admin)), check


def scenario_serial_transfer(w):
    from apps.inventory.models import PartItem

    part = w.make_serial_part("SN-303", "Блок серийный")
    item = w.make_items(part, w.loc_a, 1)[0]

    def op():
        return perform_stock_transfer(
            part=part, part_item=PartItem.objects.get(pk=item.pk), from_location=w.loc_a,
            to_location=w.loc_b, quantity="1", stock_state=StockTransfer.StockState.SERIAL,
            token="fault-serial-move", by=w.admin,
        )

    def check():
        moved = PartItem.objects.get(pk=item.pk)
        assert moved.current_location_id == w.loc_b.pk
        assert moved.status == PartItem.Status.AVAILABLE
        assert StockTransfer.objects.count() == 1

    return op, check


STOCK_OUT = ["after_stock_read", "after_stock_change", "after_movement", "after_balance"]
MATRIX = [
    (scenario_manual_sale, [*STOCK_OUT, _status_stage("sales_sale")]),
    (scenario_quick_action_sale, [
        _header_stage("sales_sale"), _header_stage("sales_saleline"), *STOCK_OUT,
        _status_stage("sales_sale"), "after_action",
    ]),
    (scenario_quick_action_repair, [
        _header_stage("repairs_repairorder"), _header_stage("repairs_repairissueline"),
        *STOCK_OUT, _status_stage("repairs_repairorder"), "after_action",
    ]),
    (scenario_cart_sale, [*STOCK_OUT, _status_stage("sales_sale"), "after_action"]),
    (scenario_reservation_to_sale, [
        _status_stage("sales_reservation"), *STOCK_OUT, _status_stage("sales_sale"),
    ]),
    (scenario_customer_request_sale, [*STOCK_OUT, _status_stage("sales_sale")]),
    (scenario_repair, [*STOCK_OUT, _status_stage("repairs_repairorder")]),
    (scenario_reservation_activate, [
        "after_stock_read", "after_balance", _status_stage("sales_reservation"),
    ]),
    (scenario_reservation_cancel, [_status_stage("sales_reservation"), "after_balance"]),
    (scenario_reservation_line_remove, ["after_balance"]),
    (scenario_sale_cancel, [
        "after_stock_change", "after_movement", "after_balance", _status_stage("sales_sale"),
    ]),
    (scenario_sale_line_cancel, [
        _header_stage("returns_stockreturn"), "after_stock_change", "after_movement",
        _status_stage("returns_stockreturn"),
    ]),
    (scenario_repair_cancel, [
        "after_stock_change", "after_movement", _status_stage("repairs_repairorder"),
    ]),
    (scenario_sale_return, [
        "after_stock_change", "after_movement", _status_stage("returns_stockreturn"),
    ]),
    (scenario_repair_return, [
        "after_stock_change", "after_movement", "after_action",
        _status_stage("returns_stockreturn"),
    ]),
    (scenario_return_cancel, [
        "after_stock_change", "after_movement", _status_stage("returns_stockreturn"),
    ]),
    (scenario_write_off, [*STOCK_OUT, _status_stage("writeoffs_writeoffdocument")]),
    (scenario_write_off_cancel, [
        "after_stock_change", "after_movement", _status_stage("writeoffs_writeoffdocument"),
    ]),
    (scenario_quick_write_off, [
        _header_stage("writeoffs_writeoffdocument"), _header_stage("writeoffs_writeoffline"),
        "after_stock_change", "after_movement", _status_stage("writeoffs_writeoffdocument"),
    ]),
    (scenario_transfer, [
        _header_stage("inventory_stocktransfer"), "after_stock_change", "after_stock_insert",
        "after_movement", "after_balance",
    ]),
    (scenario_receipt, [
        _header_stage("procurement_batch"), _header_stage("procurement_batchline"),
        "after_stock_insert", "after_movement", "after_balance",
        _status_stage("receipts_receipt"),
    ]),
    (scenario_serial_sale, [
        "after_item_change", "after_movement", "after_balance", _status_stage("sales_sale"),
    ]),
    (scenario_serial_sale_cancel, [
        "after_item_change", "after_movement", _status_stage("sales_sale"),
    ]),
    (scenario_serial_receipt, [
        "after_item_insert", "after_item_change", "after_movement",
        _status_stage("receipts_receipt"),
    ]),
    (scenario_serial_transfer, [
        _header_stage("inventory_stocktransfer"), "after_item_change", "after_movement",
    ]),
    (scenario_inventory_count, [
        "after_stock_change", "after_movement", _status_stage("stocktaking_inventorycountdocument"),
    ]),
]


CASES = [
    pytest.param(
        scenario, stage,
        id=f"{scenario.__name__[9:]}-{stage if isinstance(stage, str) else stage[2]}",
    )
    for scenario, stages in MATRIX
    for stage in stages
]


@pytest.mark.parametrize(("scenario", "stage"), CASES)
def test_injected_failure_leaves_no_effect_and_a_retry_applies_it_once(scenario, stage):
    world = World()
    operation, check = scenario(world)
    predicate, before = STAGES[stage] if isinstance(stage, str) else stage[:2]
    clean = snapshot()

    with fault_at(predicate, before=before) as hits, pytest.raises(InjectedFault):
        operation()
    assert hits["n"] == 1, "the injection point was never reached"
    assert snapshot() == clean, "a failed operation left a committed business effect"

    operation()
    check()
    after = snapshot()

    try:
        operation()
    except DOMAIN_ERRORS:
        pass
    assert snapshot() == after, "a repeated operation produced a second business effect"
