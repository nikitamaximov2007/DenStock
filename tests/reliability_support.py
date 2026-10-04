"""Общий склад для сценариев надёжности (Phase 2): документы, гонки, сбои.

Здесь только посев данных и средства наблюдения. Все складские изменения в
сценариях идут через обычные сервисы, поэтому проверяется настоящий код.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from threading import Barrier, BrokenBarrierError
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.db.models import Sum

from apps.actions.models import WarehouseAction
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from tests.customs_support import remember_customs

PASSWORD = "parol-12345"


class World:
    """Две детали, три ячейки и способ завести лот с честной себестоимостью."""

    def __init__(self):
        self.admin = get_user_model().objects.create_superuser(
            username="reliability-boss", password=PASSWORD
        )
        self.supplier = Supplier.objects.create(name="ООО Надёжность")
        self.category = Category.objects.create(name="Надёжность")
        self.unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
        self.loc_a = StorageLocation.objects.create(
            name="Ячейка A", code="S07-D01-C01", storage_allowed=True, is_active=True
        )
        self.loc_b = StorageLocation.objects.create(
            name="Ячейка B", code="S07-D01-C02", storage_allowed=True, is_active=True
        )
        self.loc_c = StorageLocation.objects.create(
            name="Ячейка C", code="S07-D01-C03", storage_allowed=True, is_active=True
        )
        self.part_x = self.make_part("RX-100", "Ремень X")
        self.part_y = self.make_part("RY-200", "Фильтр Y")

    def make_part(self, number, name, *, price="100"):
        part = PartType.objects.create(
            name=name, category=self.category, unit=self.unit,
            tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal(price),
        )
        PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM,
                                  is_primary=True)
        remember_customs(part)
        return part

    def make_serial_part(self, number, name, *, price="500"):
        part = self.make_part(number, name, price=price)
        part.tracking_mode = PartType.TrackingMode.SERIAL
        part.save(update_fields=["tracking_mode"])
        return part

    def make_items(self, part, location, count) -> list[PartItem]:
        """Serial items received the normal way: a posted receipt."""
        from apps.receipts.services import add_line, create_receipt, post_receipt

        receipt = create_receipt(supplier=self.supplier, by=self.admin)
        add_line(receipt, part_type=part, quantity=str(count), unit_cost_rub=Decimal("10"),
                 location=location)
        post_receipt(receipt, by=self.admin)
        return list(
            PartItem.objects.filter(
                part_type=part, current_location=location, status=PartItem.Status.AVAILABLE
            ).order_by("pk")
        )

    def make_lot(self, part, location, quantity, *, cost="10") -> StockLot:
        batch = Batch.objects.create(supplier=self.supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch, part_type=part, quantity=Decimal(str(quantity)),
            unit_cost_currency=Decimal(cost),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, self.admin)
        line.refresh_from_db()
        lot = create_stock_lot(line, location, Decimal(str(quantity)))
        receive_stock_lot(lot, by=self.admin)
        return StockLot.objects.get(pk=lot.pk)


# --- Fresh observation of the database ------------------------------------------------


def lot_qty(lot) -> Decimal:
    return StockLot.objects.get(pk=lot.pk).quantity


def part_physical(part) -> Decimal:
    """Physical quantity of a bulk part across every cell, read fresh."""
    return StockLot.objects.filter(
        part_type=part,
        status__in=[StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE],
    ).aggregate(s=Sum("quantity"))["s"] or Decimal("0")


def movements(**filters):
    return StockMovement.objects.filter(**filters)


def movement_count(**filters) -> int:
    return movements(**filters).count()


def actions_count(**filters) -> int:
    return WarehouseAction.objects.filter(**filters).count()


def balance_physical(lot) -> Decimal:
    row = StockBalance.objects.filter(batch_line_id=lot.batch_line_id,
                                      location_id=lot.location_id).first()
    return row.quantity_physical if row else Decimal("0")


def item_status(item) -> str:
    return PartItem.objects.get(pk=item.pk).status


# --- Forced interleavings ---------------------------------------------------------------


class Rendezvous:
    """Meet once per participating thread; never hang a correctly serialized run.

    Placed right after a thread takes its first lock, it makes both threads
    hold their first lock before either asks for the second. When the code
    under test serializes correctly, one thread blocks before reaching the
    meeting point; the other gives up waiting after ``timeout`` and goes on.
    """

    def __init__(self, parties=2, timeout=3.0):
        self.barrier = Barrier(parties, timeout=timeout)
        self.local = threading.local()

    def __call__(self):
        if getattr(self.local, "met", False):
            return
        self.local.met = True
        try:
            self.barrier.wait()
        except BrokenBarrierError:
            pass


@contextmanager
def pause_after(target, name, rendezvous, *, only=None):
    """Call the original, then meet: the caller still holds what it locked."""
    original = getattr(target, name)

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if only is None or only(*args, **kwargs):
            rendezvous()
        return result

    with mock.patch.object(target, name, wrapper):
        yield


@contextmanager
def pause_before(target, name, rendezvous, *, only=None):
    original = getattr(target, name)

    def wrapper(*args, **kwargs):
        if only is None or only(*args, **kwargs):
            rendezvous()
        return original(*args, **kwargs)

    with mock.patch.object(target, name, wrapper):
        yield


def race(*calls):
    """Run each zero-argument callable in its own thread and connection."""
    start = Barrier(len(calls))

    def runner(fn):
        close_old_connections()
        try:
            start.wait(20)
            return fn()
        except Exception as exc:  # noqa: BLE001 - the test inspects every outcome
            return exc
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(runner, fn) for fn in calls]
        return [future.result() for future in futures]


def assert_no_unexpected(results, expected_errors=()):
    """No deadlock, no database error, no crash: only results or domain refusals."""
    unexpected = [
        r for r in results
        if isinstance(r, Exception) and not isinstance(r, expected_errors)
    ]
    assert not unexpected, f"unexpected outcome: {unexpected!r}"


@contextmanager
def pause_on_sql(matches, rendezvous):
    """Meet right after this thread's connection runs a matching statement.

    Lock points are statements, not functions: the pause sits exactly where a
    row lock was just taken, before and after any refactoring of the caller.
    Must be entered inside the thread whose connection it watches.
    """
    from django.db import connection

    def wrapper(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if matches(sql):
            rendezvous()
        return result

    with connection.execute_wrapper(wrapper):
        yield


def locks(table):
    """``SELECT ... FROM "table" ... FOR UPDATE`` as a statement predicate."""
    marker = f'FROM "{table}"'

    def matches(sql):
        return "FOR UPDATE" in sql and marker in sql

    return matches


def reads(table):
    marker = f'FROM "{table}"'

    def matches(sql):
        return sql.lstrip().upper().startswith("SELECT") and marker in sql

    return matches


class Signal:
    """A one-shot event a paused thread waits on, bounded by a timeout."""

    def __init__(self, timeout=3.0):
        self.event = threading.Event()
        self.timeout = timeout
        self.local = threading.local()

    def set(self):
        self.event.set()

    def wait_once(self):
        if getattr(self.local, "waited", False):
            return
        self.local.waited = True
        self.event.wait(self.timeout)
