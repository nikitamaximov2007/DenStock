"""Reliability Phase 2: real PostgreSQL races on the warehouse documents.

Every scenario runs its operations in separate threads and connections. Where
the defect is an ordering one, the threads are stopped right after the lock
that matters (``pause_on_sql``), so the interleaving is forced, not hoped for.
After each race the database is read fresh and the stock, document and
history invariants are asserted, not just the outcome of the calls.
"""
import time
from decimal import Decimal

import pytest
from django.db import connection

from apps.actions.cart import (
    add_scan,
    complete_cart,
    load_cart,
    open_cart,
    remove_row,
)
from apps.actions.services import ActionError
from apps.inventory.models import StockLot, StockMovement, StockTransfer
from apps.inventory.services import InventoryError, perform_stock_transfer
from apps.receipts.models import Receipt, ReceiptLine
from apps.receipts.services import (
    ReceiptError,
    add_line,
    create_receipt,
    post_receipt,
    update_line,
)
from apps.repairs.models import RepairOrder
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
    complete_return,
    create_return,
)
from apps.sales.models import Reservation, Sale, SaleLine
from apps.sales.services import (
    ReservationError,
    SaleError,
    activate_reservation,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    cancel_sale,
    cancel_sale_line_quantity,
    complete_sale,
    create_reservation,
    create_sale,
)
from apps.stocktaking.services import (
    StocktakingError,
    add_stock_lot_count_line,
    complete_inventory_count,
    create_inventory_count,
    update_counted_quantity,
)
from tests.reliability_support import (
    Rendezvous,
    Signal,
    World,
    assert_no_unexpected,
    locks,
    lot_qty,
    movement_count,
    part_physical,
    pause_on_sql,
    race,
    reads,
)

pytestmark = [
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="PostgreSQL concurrency integration test"
    ),
]


@pytest.fixture
def world():
    return World()


def _sale(world, *lots, qty="1"):
    sale = create_sale(customer_name="Клиент гонки", by=world.admin)
    for lot in lots:
        add_stock_lot_to_sale(sale, lot, Decimal(qty), unit_price=Decimal("100"), by=world.admin)
    return sale


def _repair(world, *lots, qty="1"):
    order = create_repair_order(customer_name="Ремонт гонки", by=world.admin)
    for lot in lots:
        add_stock_lot_to_repair_order(order, lot, Decimal(qty), by=world.admin)
    return order


def _paused(fn, matches, meet):
    def run():
        with pause_on_sql(matches, meet):
            return fn()

    return run


# --- Lock order: documents that share lots ---------------------------------------------


def test_two_sales_sharing_lots_in_opposite_order_never_deadlock(world):
    """SALE-1 / LOCK-1: both complete, each consumes exactly its own quantity."""
    first = world.make_lot(world.part_x, world.loc_a, 5)
    second = world.make_lot(world.part_y, world.loc_b, 5)
    forward = _sale(world, first, second)
    backward = _sale(world, second, first)
    meet = Rendezvous()

    results = race(
        _paused(lambda: complete_sale(forward, by=world.admin), locks("inventory_stocklot"), meet),
        _paused(lambda: complete_sale(backward, by=world.admin), locks("inventory_stocklot"), meet),
    )

    assert_no_unexpected(results, (SaleError,))
    assert [Sale.objects.get(pk=s.pk).status for s in (forward, backward)] == [
        Sale.Status.COMPLETED, Sale.Status.COMPLETED,
    ]
    assert lot_qty(first) == lot_qty(second) == Decimal("3")
    assert movement_count(document_type="sale") == 4


def test_sale_and_repair_sharing_lots_in_opposite_order_never_deadlock(world):
    first = world.make_lot(world.part_x, world.loc_a, 5)
    second = world.make_lot(world.part_y, world.loc_b, 5)
    sale = _sale(world, first, second)
    order = _repair(world, second, first)
    meet = Rendezvous()

    results = race(
        _paused(lambda: complete_sale(sale, by=world.admin), locks("inventory_stocklot"), meet),
        _paused(lambda: complete_repair_order(order, by=world.admin),
                locks("inventory_stocklot"), meet),
    )

    assert_no_unexpected(results, (SaleError, RepairError))
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    assert RepairOrder.objects.get(pk=order.pk).status == RepairOrder.Status.COMPLETED
    assert lot_qty(first) == lot_qty(second) == Decimal("3")


def test_transfer_racing_a_sale_on_the_same_lot_never_deadlocks(world):
    """MOVE-1: total physical quantity is preserved; the sale consumes exactly once."""
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    sale = _sale(world, lot)
    meet = Rendezvous()

    def transfer():
        return perform_stock_transfer(
            part=world.part_x, from_location=world.loc_a, to_location=world.loc_b,
            quantity="2", stock_state=StockLot.Status.AVAILABLE, token="race-move-1",
            by=world.admin,
        )

    results = race(
        _paused(lambda: complete_sale(sale, by=world.admin), locks("inventory_stocklot"), meet),
        _paused(transfer, locks("warehouse_storagelocation"), meet),
    )

    assert_no_unexpected(results, (SaleError, InventoryError))
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    assert StockTransfer.objects.filter(token="race-move-1").count() == 1
    assert part_physical(world.part_x) == Decimal("4")
    in_a = StockLot.objects.filter(part_type=world.part_x, location=world.loc_a)
    in_b = StockLot.objects.filter(part_type=world.part_x, location=world.loc_b)
    assert sum(lot.quantity for lot in in_a) == Decimal("2")
    assert sum(lot.quantity for lot in in_b) == Decimal("2")


def test_reservation_activation_racing_a_sale_on_the_same_lot_never_deadlocks(world):
    """RES-2: the reservation holds what the sale left; nothing is consumed twice."""
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    reservation = create_reservation(customer_name="Бронь гонки", by=world.admin)
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=world.admin)
    sale = _sale(world, lot)
    meet = Rendezvous()

    results = race(
        _paused(lambda: complete_sale(sale, by=world.admin), locks("inventory_stocklot"), meet),
        _paused(lambda: activate_reservation(reservation, by=world.admin),
                locks("warehouse_storagelocation"), meet),
    )

    assert_no_unexpected(results, (SaleError, ReservationError))
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    assert Reservation.objects.get(pk=reservation.pk).status == Reservation.Status.ACTIVE
    assert lot_qty(lot) == Decimal("4")


def test_whole_sale_cancellation_racing_a_line_cancellation_never_deadlocks(world):
    """CANCEL-1: stock comes back exactly once, whichever cancellation wins."""
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    sale = _sale(world, lot, qty="3")
    complete_sale(sale, by=world.admin)
    line = SaleLine.objects.get(sale=sale)
    meet = Rendezvous()

    results = race(
        _paused(
            lambda: cancel_sale(sale, by=world.admin, reason="Ошибка", author="Денис"),
            locks("sales_sale"), meet,
        ),
        _paused(
            lambda: cancel_sale_line_quantity(
                line, "1", reason="Ошибка", author="Денис", by=world.admin
            ),
            locks("sales_saleline"), meet,
        ),
    )

    assert_no_unexpected(results, (SaleError,))
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.CANCELED
    # Whatever order: 3 sold, 3 back in total (partial 1 + remaining 2, or all 3).
    assert lot_qty(lot) == Decimal("5")


# --- Compensation applied twice ----------------------------------------------------------


def test_a_return_drafted_while_the_sale_is_being_cancelled_cannot_return_stock_twice(world):
    """RETURN-1 / CANCEL-1: a return of a cancelled sale is refused, stock stays exact."""
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    sale = _sale(world, lot, qty="2")
    complete_sale(sale, by=world.admin)
    line = SaleLine.objects.get(sale=sale)
    # The cancellation pauses after its last lock; the draft's COMMIT then
    # waits for it (deferred foreign keys) and lands right after it.
    drafted = Signal(timeout=2)
    cancel_paused = Signal(timeout=5)
    holder = {}

    def cancel():
        def on_lock():
            cancel_paused.set()
            drafted.wait_once()

        with pause_on_sql(locks("catalog_parttype"), on_lock):
            return cancel_sale(sale, by=world.admin, reason="Ошибка", author="Денис")

    def draft():
        cancel_paused.event.wait(5)
        ret = create_return(source=Sale.objects.get(pk=sale.pk), reason="Клиент вернул")
        add_sale_line_return(
            ret, line, Decimal("2"), to_location=world.loc_a,
            restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        )
        holder["return"] = ret
        drafted.set()
        return ret

    results = race(cancel, draft)
    assert_no_unexpected(results, (SaleError, ReturnError))
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.CANCELED
    assert lot_qty(lot) == Decimal("5")

    with pytest.raises(ReturnError):
        complete_return(holder["return"], by=world.admin)
    assert lot_qty(lot) == Decimal("5")
    assert StockReturn.objects.get(pk=holder["return"].pk).status == StockReturn.Status.DRAFT
    assert movement_count(document_type="return", stock_lot_id=lot.pk) == 0


def test_a_repair_return_drafted_while_the_repair_is_being_cancelled_is_refused(world):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    order = _repair(world, lot, qty="2")
    complete_repair_order(order, by=world.admin)
    repair_line = order.lines.get()
    drafted = Signal(timeout=2)
    cancel_paused = Signal(timeout=5)
    holder = {}

    def cancel():
        def on_lock():
            cancel_paused.set()
            drafted.wait_once()

        with pause_on_sql(locks("catalog_parttype"), on_lock):
            return cancel_repair_order(order, by=world.admin, reason="Ошибка", author="Денис")

    def draft():
        cancel_paused.event.wait(5)
        ret = create_return(source=RepairOrder.objects.get(pk=order.pk), reason="Вернули")
        add_repair_line_return(
            ret, repair_line, Decimal("2"), to_location=world.loc_a,
            restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        )
        holder["return"] = ret
        drafted.set()
        return ret

    results = race(cancel, draft)
    assert_no_unexpected(results, (RepairError, ReturnError))
    assert RepairOrder.objects.get(pk=order.pk).status == RepairOrder.Status.CANCELED
    assert lot_qty(lot) == Decimal("5")

    with pytest.raises(ReturnError):
        complete_return(holder["return"], by=world.admin)
    assert lot_qty(lot) == Decimal("5")


# --- Draft edits racing posting / completion ---------------------------------------------


def _receipt(world, quantity="3"):
    receipt = create_receipt(supplier=world.supplier, by=world.admin)
    line = add_line(
        receipt, part_type=world.part_x, quantity=quantity, unit_cost_rub=Decimal("50"),
        location=world.loc_a,
    )
    return receipt, line


def _post_paused(world, receipt, editor_done, matches):
    def on_point():
        editor_done.wait_once()

    def post():
        with pause_on_sql(matches, on_point):
            return post_receipt(Receipt.objects.get(pk=receipt.pk), by=world.admin)

    return post


def test_a_line_added_while_the_receipt_posts_is_never_left_unreceived(world):
    """RECEIVE-1: every line of a posted receipt was received, exactly once."""
    receipt, _line = _receipt(world)
    editor_done = Signal(timeout=2)

    def add():
        time.sleep(0.5)
        try:
            return add_line(
                Receipt.objects.get(pk=receipt.pk), part_type=world.part_x, quantity="7",
                unit_cost_rub=Decimal("50"), location=world.loc_a,
            )
        finally:
            editor_done.set()

    # Posting has read its lines and locked its cells when the line arrives.
    results = race(
        _post_paused(world, receipt, editor_done, locks("warehouse_storagelocation")), add
    )
    assert_no_unexpected(results, (ReceiptError,))
    assert Receipt.objects.get(pk=receipt.pk).status == Receipt.Status.POSTED
    lines = ReceiptLine.objects.filter(receipt=receipt)
    assert all(line.batch_line_id for line in lines), "posted line without stock"
    assert part_physical(world.part_x) == sum(line.quantity for line in lines)


def test_a_line_edited_while_the_receipt_posts_keeps_document_and_stock_equal(world):
    receipt, line = _receipt(world)
    editor_done = Signal(timeout=3)

    def edit():
        time.sleep(0.5)
        try:
            return update_line(
                ReceiptLine.objects.select_related("receipt").get(pk=line.pk),
                part_type=world.part_x, quantity="9", unit_cost_rub=Decimal("50"),
                location=world.loc_a,
            )
        finally:
            editor_done.set()

    results = race(_post_paused(world, receipt, editor_done, reads("receipts_receiptline")), edit)
    assert_no_unexpected(results, (ReceiptError,))
    posted = ReceiptLine.objects.get(pk=line.pk)
    assert part_physical(world.part_x) == posted.quantity


def test_a_cart_row_removed_while_the_cart_completes_keeps_the_sale_whole(world):
    """SALE-1 / HISTORY-1: a completed sale keeps every line it consumed stock for."""
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    cart = open_cart("sale", by=world.admin)
    add_scan(cart, world.part_x, world.loc_a, quantity=Decimal("2"), by=world.admin)
    completing = Signal(timeout=5)

    def complete():
        def on_update():
            completing.set()
            time.sleep(1.0)

        def matches(sql):
            return sql.lstrip().upper().startswith('UPDATE "SALES_SALE" ') and "status" in sql

        with pause_on_sql(matches, on_update):
            return complete_cart(
                Sale.objects.get(pk=cart.pk), customer_comment="Клиент", by=world.admin
            )

    def remove():
        completing.event.wait(5)
        stale = load_cart("sale", cart.pk)
        if stale is None:
            return None
        return remove_row(stale, world.part_x, world.loc_a, by=world.admin)

    results = race(complete, remove)
    assert_no_unexpected(results, (ActionError,))
    sale = Sale.objects.get(pk=cart.pk)
    assert sale.status == Sale.Status.COMPLETED
    sold = sum(line.quantity for line in SaleLine.objects.filter(sale=sale))
    assert sold == Decimal("2")
    assert lot_qty(lot) == Decimal("3")
    assert movement_count(document_type="sale", document_id=sale.pk) == 1


# --- Required race matrix that existing suites do not cover ------------------------------


def test_sale_and_reservation_race_for_the_last_unit(world):
    lot = world.make_lot(world.part_x, world.loc_a, 1)
    sale = _sale(world, lot)
    reservation = create_reservation(customer_name="Бронь", by=world.admin)
    add_stock_lot_to_reservation(reservation, lot, Decimal("1"), by=world.admin)

    results = race(
        lambda: complete_sale(sale, by=world.admin),
        lambda: activate_reservation(reservation, by=world.admin),
    )
    assert_no_unexpected(results, (SaleError, ReservationError))
    wins = [r for r in results if not isinstance(r, Exception)]
    assert len(wins) == 1
    sold = Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    held = Reservation.objects.get(pk=reservation.pk).status == Reservation.Status.ACTIVE
    assert sold != held
    assert lot_qty(lot) == (Decimal("0") if sold else Decimal("1"))


def test_two_reservations_race_for_the_last_unit(world):
    lot = world.make_lot(world.part_x, world.loc_a, 1)
    first = create_reservation(customer_name="Первая", by=world.admin)
    second = create_reservation(customer_name="Вторая", by=world.admin)
    for reservation in (first, second):
        add_stock_lot_to_reservation(reservation, lot, Decimal("1"), by=world.admin)

    results = race(
        lambda: activate_reservation(first, by=world.admin),
        lambda: activate_reservation(second, by=world.admin),
    )
    assert_no_unexpected(results, (ReservationError,))
    active = Reservation.objects.filter(
        pk__in=[first.pk, second.pk], status=Reservation.Status.ACTIVE
    ).count()
    assert active == 1
    assert lot_qty(lot) == Decimal("1")


def test_reservation_line_removal_races_conversion_to_sale(world):
    from apps.sales.services import create_sale_from_reservation, remove_reservation_line

    lot = world.make_lot(world.part_x, world.loc_a, 5)
    reservation = create_reservation(customer_name="Бронь", by=world.admin)
    rline = add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=world.admin)
    activate_reservation(reservation, by=world.admin)
    sale = create_sale_from_reservation(reservation, by=world.admin)

    results = race(
        lambda: complete_sale(sale, by=world.admin),
        lambda: remove_reservation_line(rline, by=world.admin),
    )
    assert_no_unexpected(results, (SaleError, ReservationError))
    status = Sale.objects.get(pk=sale.pk).status
    consumed = Decimal("5") - lot_qty(lot)
    assert consumed == (Decimal("2") if status == Sale.Status.COMPLETED else Decimal("0"))
    assert movement_count(document_type="sale") == (1 if consumed else 0)


@pytest.mark.parametrize("consumer", ["sale", "repair"])
def test_inventory_count_racing_a_consumer_never_resurrects_stock(world, consumer):
    """COUNT-1: a count taken before the consumption cannot overwrite it."""
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    count = create_inventory_count(scope_location=world.loc_a, by=world.admin)
    count_line = add_stock_lot_count_line(count, lot, by=world.admin)
    update_counted_quantity(count_line, Decimal("5"), by=world.admin)
    document = _sale(world, lot, qty="2") if consumer == "sale" else _repair(world, lot, qty="2")
    consume = (
        (lambda: complete_sale(document, by=world.admin))
        if consumer == "sale"
        else (lambda: complete_repair_order(document, by=world.admin))
    )

    results = race(consume, lambda: complete_inventory_count(count, by=world.admin))
    assert_no_unexpected(results, (SaleError, RepairError, StocktakingError))
    consumed = not isinstance(results[0], Exception)
    counted = not isinstance(results[1], Exception)
    assert consumed
    # The count either ran first (no change: 5 counted == 5 live) or is refused as stale.
    assert lot_qty(lot) == Decimal("3")
    assert movement_count(document_type="inventory_count") == 0
    assert counted in (True, False)


def test_quick_action_cancel_racing_its_own_sale_cancellation_restores_once(world):
    from apps.actions.services import cancel_warehouse_action, perform_action

    lot = world.make_lot(world.part_x, world.loc_a, 5)
    action = perform_action(
        part=world.part_x, location=world.loc_a, action_type="sale", quantity="2",
        customer_comment="Клиент", by=world.admin, request_token="race-qa-1",
    )
    sale = Sale.objects.get(pk=action.sale_id)

    results = race(
        lambda: cancel_warehouse_action(action, by=world.admin, reason="Ошибка"),
        lambda: cancel_sale(sale, by=world.admin, reason="Ошибка", author="Денис"),
    )
    assert_no_unexpected(results, (ActionError, SaleError))
    assert lot_qty(lot) == Decimal("5")
    assert StockMovement.objects.filter(
        document_type="sale", document_id=sale.pk, movement_type__startswith="return"
    ).count() <= 1


def test_write_off_and_sale_sharing_lots_in_opposite_order_never_deadlock(world):
    from apps.writeoffs.models import WriteOffDocument
    from apps.writeoffs.services import (
        WriteOffError,
        add_stock_lot_to_write_off,
        complete_write_off,
        create_write_off,
    )

    first = world.make_lot(world.part_x, world.loc_a, 5)
    second = world.make_lot(world.part_y, world.loc_b, 5)
    sale = _sale(world, first, second)
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=world.admin)
    for lot in (second, first):
        add_stock_lot_to_write_off(doc, lot, Decimal("1"))
    meet = Rendezvous()

    results = race(
        _paused(lambda: complete_sale(sale, by=world.admin), locks("inventory_stocklot"), meet),
        _paused(lambda: complete_write_off(doc, by=world.admin),
                locks("inventory_stocklot"), meet),
    )
    assert_no_unexpected(results, (SaleError, WriteOffError))
    assert lot_qty(first) == lot_qty(second) == Decimal("3")


def test_parallel_double_submit_of_one_line_cancellation_cancels_once(world):
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    sale = _sale(world, lot, qty="3")
    complete_sale(sale, by=world.admin)
    line = SaleLine.objects.get(sale=sale)

    def submit():
        return cancel_sale_line_quantity(
            line, "1", reason="Ошибка", author="Денис", by=world.admin, expected_remaining="3"
        )

    results = race(submit, submit)
    assert_no_unexpected(results, (SaleError,))
    assert len([r for r in results if not isinstance(r, Exception)]) == 1
    assert lot_qty(lot) == Decimal("3")
    assert StockReturn.objects.filter(status=StockReturn.Status.COMPLETED).count() == 1


def test_parallel_double_submit_of_one_quick_write_off_writes_off_once(world):
    from apps.writeoffs.models import WriteOffDocument
    from apps.writeoffs.services import WriteOffError, quick_write_off

    lot = world.make_lot(world.part_x, world.loc_a, 5)

    def submit():
        return quick_write_off(
            part=world.part_x, scanned_code="RX-100", reason="Брак", business_author="Денис",
            quantity="1", location_id=world.loc_a.pk, by=world.admin,
            request_token="parallel-quick-wo",
        )

    results = race(submit, submit)
    assert_no_unexpected(results, (WriteOffError,))
    docs = {r.pk for r in results if not isinstance(r, Exception)}
    assert len(docs) == 1
    assert WriteOffDocument.objects.count() == 1
    assert lot_qty(lot) == Decimal("4")


def test_cart_row_edit_racing_cart_completion_never_deadlocks(world):
    from apps.actions.cart import set_row_quantity

    lot = world.make_lot(world.part_x, world.loc_a, 5)
    cart = open_cart("sale", by=world.admin)
    add_scan(cart, world.part_x, world.loc_a, quantity=Decimal("2"), by=world.admin)

    results = race(
        lambda: complete_cart(
            Sale.objects.get(pk=cart.pk), customer_comment="Клиент", by=world.admin
        ),
        lambda: set_row_quantity(
            Sale.objects.get(pk=cart.pk), world.part_x, world.loc_a, 3, by=world.admin
        ),
    )
    assert_no_unexpected(results, (ActionError,))
    sale = Sale.objects.get(pk=cart.pk)
    assert sale.status == Sale.Status.COMPLETED
    sold = sum(line.quantity for line in SaleLine.objects.filter(sale=sale))
    assert lot_qty(lot) == Decimal("5") - sold
    assert movement_count(document_type="sale", document_id=sale.pk) == 1


def _found_posting(world, part, location, token):
    from apps.inventory.services import post_found_stock_group

    return lambda: post_found_stock_group(
        entries=[{
            "source": "warehouse", "source_id": part.pk,
            "exact_number": "RX-100", "quantity": 1,
        }],
        location=location, token=token, by=world.admin,
    )


def test_found_stock_posting_racing_a_sale_in_the_same_cell_never_deadlocks(world):
    """RECEIVE-1 / SALE-1: +1 found and -1 sold, each exactly once."""
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    sale = _sale(world, lot)
    meet = Rendezvous()

    results = race(
        _paused(lambda: complete_sale(sale, by=world.admin), locks("inventory_stocklot"), meet),
        _paused(_found_posting(world, world.part_x, world.loc_a, "found-race-1"),
                locks("warehouse_storagelocation"), meet),
    )
    assert_no_unexpected(results, (SaleError, InventoryError))
    assert Sale.objects.get(pk=sale.pk).status == Sale.Status.COMPLETED
    assert lot_qty(lot) == Decimal("5")
    assert movement_count(document_type="found_addition") == 1


def test_found_stock_posting_racing_a_transfer_from_the_same_cell_never_deadlocks(world):
    """MOVE-1: total physical quantity = initial + found, nothing lost or doubled."""
    world.make_lot(world.part_x, world.loc_a, 5)
    meet = Rendezvous()

    def transfer():
        return perform_stock_transfer(
            part=world.part_x, from_location=world.loc_a, to_location=world.loc_b,
            quantity="2", stock_state=StockLot.Status.AVAILABLE, token="found-move-1",
            by=world.admin,
        )

    results = race(
        _paused(transfer, locks("inventory_stocklot"), meet),
        _paused(_found_posting(world, world.part_x, world.loc_a, "found-race-2"),
                locks("warehouse_storagelocation"), meet),
    )
    assert_no_unexpected(results, (InventoryError,))
    assert part_physical(world.part_x) == Decimal("6")
    in_b = StockLot.objects.get(part_type=world.part_x, location=world.loc_b)
    assert in_b.quantity == Decimal("2")


def test_inventory_count_racing_a_found_stock_posting_never_hides_the_addition(world):
    """COUNT-1: a count snapshotted before the +1 cannot erase it."""
    lot = world.make_lot(world.part_x, world.loc_a, 5)
    count = create_inventory_count(scope_location=world.loc_a, by=world.admin)
    count_line = add_stock_lot_count_line(count, lot, by=world.admin)
    update_counted_quantity(count_line, Decimal("5"), by=world.admin)

    results = race(
        _found_posting(world, world.part_x, world.loc_a, "found-count-1"),
        lambda: complete_inventory_count(count, by=world.admin),
    )
    assert_no_unexpected(results, (InventoryError, StocktakingError))
    assert not isinstance(results[0], Exception)
    # Found first: the count is stale and refused. Count first: no change, then +1.
    assert lot_qty(lot) == Decimal("6")
    assert movement_count(document_type="inventory_count") == 0
