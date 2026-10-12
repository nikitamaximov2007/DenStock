from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.counting.models import InventoryCountingSession
from apps.counting.services import (
    CountingError,
    post_session,
    record_scan,
    restart_session,
    start_session,
)
from apps.inventory.models import NumberSequence, StockLocationLock, StockMovement
from apps.inventory.services import InventoryError
from apps.receipts.services import add_line, create_receipt, post_receipt
from apps.stocktaking.models import SectionRecount
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"),
]


@pytest.fixture
def cancel_scene():
    NumberSequence.objects.get_or_create(
        key="receipt", defaults={"prefix": "ПОС-", "last_value": 0}
    )
    NumberSequence.objects.get_or_create(
        key="inventory_count", defaults={"prefix": "IC-", "last_value": 0}
    )
    user_model = get_user_model()
    first = user_model.objects.create_superuser("cancel-race-a", password="test")
    second = user_model.objects.create_superuser("cancel-race-b", password="test")
    unit, _ = Unit.objects.get_or_create(name="Штука")
    part = PartType.objects.create(
        name="Race cancellation part",
        category=Category.objects.create(name="Cancellation race"),
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=part, value="P1-CANCEL-RACE", kind=PartNumber.Kind.OEM)
    location = get_or_create_location("S01-D03-C08", name="Cancellation race cell")
    supplier, _ = Supplier.objects.get_or_create(
        name="Cancellation race supplier", defaults={"is_active": True}
    )
    return first, second, part, location, supplier


def test_two_operators_racing_restart_create_one_replacement(cancel_scene):
    first, second, _part, location, _supplier = cancel_scene
    session = start_session(location=location, by=first)
    barrier = Barrier(2)

    def restart(user):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            locked = InventoryCountingSession.objects.get(pk=session.pk)
            return restart_session(locked, by=user, reason="Дублированный запрос оператора")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        replacements = list(pool.map(restart, (first, second)))

    session.refresh_from_db()
    assert replacements[0].pk == replacements[1].pk
    assert session.status == InventoryCountingSession.Status.CANCELLED
    assert session.replacement_recount_id == replacements[0].pk
    assert SectionRecount.objects.filter(
        section_code=location.code,
        status__in=["draft", "counting", "ready", "applying"],
    ).count() == 1


def test_cancel_racing_post_has_one_terminal_state_and_no_duplicate_stock(cancel_scene):
    first, second, _part, location, _supplier = cancel_scene
    session = start_session(location=location, by=first)
    record_scan(session, "P1-CANCEL-RACE", by=first)
    before = StockMovement.objects.count()
    barrier = Barrier(2)

    def cancel():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            from apps.counting.services import cancel_session

            cancel_session(
                InventoryCountingSession.objects.get(pk=session.pk),
                by=second,
                reason="Отмена параллельно проведению",
            )
            return "cancelled"
        except CountingError:
            return "refused"
        finally:
            close_old_connections()

    def post():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            post_session(InventoryCountingSession.objects.get(pk=session.pk), by=first)
            return "posted"
        except CountingError:
            return "refused"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_result, post_result = list(pool.map(lambda fn: fn(), (cancel, post)))

    session.refresh_from_db()
    if session.status == InventoryCountingSession.Status.CANCELLED:
        assert cancel_result == "cancelled"
        assert post_result == "refused"
        assert StockMovement.objects.count() == before
    else:
        assert session.status == InventoryCountingSession.Status.POSTED
        assert post_result == "posted"
        assert cancel_result == "refused"
        assert StockMovement.objects.count() > before
        assert StockMovement.objects.filter(to_location=location).count() == 1


def test_restart_racing_stock_movement_has_a_safe_fresh_baseline(cancel_scene):
    first, second, part, location, supplier = cancel_scene
    session = start_session(location=location, by=first)
    record_scan(session, "P1-CANCEL-RACE", by=first)
    receipt = create_receipt(supplier=supplier, by=second)
    add_line(
        receipt,
        part_type=part,
        quantity=Decimal("14"),
        unit_cost_rub=Decimal("25"),
        location=location,
    )
    before = StockMovement.objects.count()
    barrier = Barrier(2)

    def restart():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            return restart_session(
                InventoryCountingSession.objects.get(pk=session.pk),
                by=first,
                reason="Перезапуск параллельно движению",
            )
        finally:
            close_old_connections()

    def move_stock():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            try:
                post_receipt(type(receipt).objects.get(pk=receipt.pk), by=second)
            except InventoryError:
                return "blocked"
            return "posted"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        replacement_future = pool.submit(restart)
        receipt_future = pool.submit(move_stock)
        replacement = replacement_future.result()
        movement_result = receipt_future.result()

    session.refresh_from_db()
    receipt.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.CANCELLED
    assert session.replacement_recount_id == replacement.pk
    assert replacement.status == SectionRecount.Status.COUNTING
    assert StockLocationLock.objects.filter(
        document_id=replacement.pk, released_at__isnull=True
    ).exists()
    snapshot_quantity = sum(
        (Decimal(row["quantity"]) for row in replacement.snapshot["lots"]), Decimal("0")
    )
    if movement_result == "posted":
        assert receipt.status == "posted"
        assert StockMovement.objects.count() == before + 1
        assert snapshot_quantity == Decimal("14")
    else:
        assert receipt.status == "draft"
        assert StockMovement.objects.count() == before
        assert snapshot_quantity == Decimal("0")
