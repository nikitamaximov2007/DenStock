"""A posted return is an exactly-once inventory document, including admin/ORM paths."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.urls import reverse

from apps.inventory.models import StockLot, StockMovement
from apps.returns.models import StockReturn, StockReturnLine
from apps.returns.services import (
    ReturnError,
    add_sale_line_return,
    cancel_return,
    complete_return,
    create_return,
)

pytest_plugins = ("tests.test_returns",)


def _draft(data, *, quantities=("1",), existing=False):
    ret = create_return(source=data["sale"], by=data["admin"])
    sources = [data["sale_lot_line"], data["sale_small_line"]]
    for index, quantity in enumerate(quantities):
        add_sale_line_return(
            ret, sources[index], Decimal(quantity),
            to_location=data["loc"] if existing else data["loc2"],
            restock_status=StockReturnLine.RestockStatus.AVAILABLE,
            by=data["admin"],
        )
    return ret


def _movements(ret):
    return list(StockMovement.objects.filter(
        document_type="stock_return", document_id=ret.pk,
        movement_type=StockMovement.MovementType.RETURN_LOT,
    ).order_by("pk").values_list("quantity", flat=True))


@pytest.mark.django_db
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("quantities", [("1",), ("1", "2")])
def test_completion_once_for_new_existing_and_multiline(data, existing, quantities):
    ret = _draft(data, quantities=quantities, existing=existing)
    first = complete_return(ret, by=data["admin"])
    lot_ids = list(ret.lines.order_by("pk").values_list("returned_lot_id", flat=True))
    before = dict(StockLot.objects.filter(pk__in=lot_ids).values_list("pk", "quantity"))
    assert first.status == StockReturn.Status.COMPLETED
    assert _movements(ret) == [Decimal(q) for q in quantities]
    assert complete_return(ret, by=data["admin"]).pk == ret.pk
    assert _movements(ret) == [Decimal(q) for q in quantities]
    assert dict(StockLot.objects.filter(pk__in=lot_ids).values_list("pk", "quantity")) == before


@pytest.mark.django_db
def test_real_admin_post_cannot_reopen_completed_return(data, client):
    ret = _draft(data)
    complete_return(ret, by=data["admin"])
    line = ret.lines.get()
    before = _movements(ret)
    quantity_before = StockLot.objects.get(pk=line.returned_lot_id).quantity
    client.force_login(data["admin"])
    response = client.post(reverse("admin:returns_stockreturn_change", args=[ret.pk]), {
        "status": StockReturn.Status.DRAFT,
        "source_type": ret.source_type,
        "source_id": ret.source_id,
        "reason": ret.reason,
        "comment": ret.comment,
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-id": str(line.pk), "lines-0-stock_return": str(ret.pk),
        "lines-0-source_sale_line": str(line.source_sale_line_id),
        "lines-0-source_repair_line": "", "lines-0-part_type": str(line.part_type_id),
        "lines-0-part_item": "", "lines-0-stock_lot": str(line.stock_lot_id),
        "lines-0-batch": str(line.batch_id), "lines-0-batch_line": str(line.batch_line_id),
        "lines-0-quantity": "99", "lines-0-to_location": str(line.to_location_id),
        "lines-0-restock_status": line.restock_status,
    })
    assert response.status_code in (200, 302)
    ret.refresh_from_db()
    line.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED
    assert line.quantity == Decimal("1")
    assert _movements(ret) == before
    assert StockLot.objects.get(pk=line.returned_lot_id).quantity == quantity_before


@pytest.mark.django_db
def test_admin_cannot_delete_posted_return(data, client):
    ret = _draft(data)
    complete_return(ret, by=data["admin"])
    client.force_login(data["admin"])
    response = client.post(
        reverse("admin:returns_stockreturn_delete", args=[ret.pk]),
        {"post": "yes"},
    )
    assert response.status_code == 403
    assert StockReturn.objects.filter(pk=ret.pk, status=StockReturn.Status.COMPLETED).exists()
    assert _movements(ret) == [Decimal("1")]


@pytest.mark.django_db
def test_model_and_bulk_orm_cannot_reopen_or_mutate_posted_evidence(data):
    ret = _draft(data)
    complete_return(ret, by=data["admin"])
    line = ret.lines.get()
    ret.status = StockReturn.Status.DRAFT
    with pytest.raises(ValidationError):
        ret.save(update_fields=["status"])
    for operation in (
        lambda: StockReturn.objects.filter(pk=ret.pk).update(status="draft"),
        lambda: StockReturn.objects.bulk_update([ret], ["status"]),
        lambda: StockReturn.objects.filter(pk=ret.pk).delete(),
    ):
        with pytest.raises(ValidationError):
            with transaction.atomic():
                operation()
    line.quantity = Decimal("2")
    for operation in (
        lambda: line.save(update_fields=["quantity"]),
        lambda: StockReturnLine.objects.filter(pk=line.pk).update(quantity=2),
        lambda: StockReturnLine.objects.bulk_update([line], ["quantity"]),
        lambda: StockReturnLine.objects.filter(pk=line.pk).delete(),
    ):
        with pytest.raises(ValidationError):
            with transaction.atomic():
                operation()
    line_pk = line.pk
    line.pk = None
    with pytest.raises(ValidationError):
        StockReturnLine.objects.bulk_create([line])
    line.pk = line_pk
    ret.refresh_from_db()
    line.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED
    assert line.quantity == Decimal("1")
    assert _movements(ret) == [Decimal("1")]


@pytest.mark.django_db
def test_legacy_draft_with_posted_movement_fails_closed(data):
    ret = _draft(data)
    complete_return(ret, by=data["admin"])
    before = _movements(ret)
    # Deliberately bypass application guards to simulate an already-corrupt legacy row.
    with connection.cursor() as cursor:
        cursor.execute("UPDATE returns_stockreturn SET status = 'draft' WHERE id = %s", [ret.pk])
    with pytest.raises(ReturnError, match="уже зачислен"):
        complete_return(ret, by=data["admin"])
    assert _movements(ret) == before


@pytest.mark.django_db
def test_cancelled_posted_return_cannot_be_reopened(data):
    ret = _draft(data)
    complete_return(ret, by=data["admin"])
    cancel_return(ret, by=data["admin"], reason="Ошибка оформления")
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.CANCELED
    ret.status = StockReturn.Status.DRAFT
    with pytest.raises(ValidationError):
        ret.save(update_fields=["status"])


@pytest.mark.django_db
def test_second_line_failure_rolls_back_entire_return_and_retry_succeeds(data):
    ret = _draft(data, quantities=("1", "2"))
    data["lot"].refresh_from_db()
    before = data["lot"].quantity
    from apps.returns import services

    original = services.return_stock_lot_quantity
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("test-only second line failure")
        return original(*args, **kwargs)

    with patch.object(services, "return_stock_lot_quantity", side_effect=fail_second):
        with pytest.raises(RuntimeError, match="test-only"):
            complete_return(ret, by=data["admin"])
    ret.refresh_from_db()
    data["lot"].refresh_from_db()
    assert ret.status == StockReturn.Status.DRAFT
    assert data["lot"].quantity == before
    assert _movements(ret) == []
    assert all(line.returned_lot_id is None for line in ret.lines.all())
    complete_return(ret, by=data["admin"])
    assert _movements(ret) == [Decimal("1"), Decimal("2")]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("quantities", [("1",), ("1", "2")])
def test_concurrent_completion_waits_and_credits_once(data, existing, quantities):
    if connection.vendor != "postgresql":
        pytest.skip("Real PostgreSQL row locks required")
    ret = _draft(data, quantities=quantities, existing=existing)
    source_lot_ids = list(ret.lines.order_by("pk").values_list("stock_lot_id", flat=True))
    original_quantities = dict(StockLot.objects.filter(
        pk__in=source_lot_ids
    ).values_list("pk", "quantity"))
    from apps.returns import services

    original = services.return_stock_lot_quantity
    first_inside = threading.Event()
    release_first = threading.Event()
    call_count = 0
    guard = threading.Lock()

    def paused_return(*args, **kwargs):
        nonlocal call_count
        with guard:
            call_count += 1
            pause = call_count == 1
        result = original(*args, **kwargs)
        if pause:
            first_inside.set()
            assert release_first.wait(timeout=10)
        return result

    def worker():
        close_old_connections()
        try:
            return complete_return(StockReturn(pk=ret.pk), by=data["admin"]).status
        finally:
            close_old_connections()

    with patch.object(services, "return_stock_lot_quantity", side_effect=paused_return):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker)
            assert first_inside.wait(timeout=10)
            second = pool.submit(worker)
            blocked = False
            for _ in range(40):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query LIKE %s", ["%returns_stockreturn%"],
                    )
                    blocked = cursor.fetchone()[0] > 0
                if blocked:
                    break
                time.sleep(0.05)
            release_first.set()
            assert blocked, "Second PostgreSQL session never waited for the header lock"
            assert first.result(timeout=15) == StockReturn.Status.COMPLETED
            assert second.result(timeout=15) == StockReturn.Status.COMPLETED
    assert _movements(ret) == [Decimal(q) for q in quantities]
    target_lot_ids = list(ret.lines.order_by("pk").values_list("returned_lot_id", flat=True))
    target_quantities = dict(StockLot.objects.filter(
        pk__in=target_lot_ids
    ).values_list("pk", "quantity"))
    for index, quantity in enumerate(quantities):
        target_id = target_lot_ids[index]
        expected = Decimal(quantity) + (
            original_quantities[source_lot_ids[index]] if existing else Decimal("0")
        )
        assert target_quantities[target_id] == expected


@pytest.mark.django_db(transaction=True)
def test_bulk_status_reset_waits_for_completion_and_then_refuses(data):
    if connection.vendor != "postgresql":
        pytest.skip("Real PostgreSQL row locks required")
    ret = _draft(data)
    from apps.returns import services

    original = services.return_stock_lot_quantity
    movement_written = threading.Event()
    release_completion = threading.Event()

    def pause_after_stock(*args, **kwargs):
        result = original(*args, **kwargs)
        movement_written.set()
        assert release_completion.wait(timeout=10)
        return result

    def complete():
        close_old_connections()
        try:
            return complete_return(StockReturn(pk=ret.pk), by=data["admin"])
        finally:
            close_old_connections()

    def reset():
        close_old_connections()
        try:
            return StockReturn.objects.filter(pk=ret.pk).update(status="draft")
        finally:
            close_old_connections()

    with patch.object(services, "return_stock_lot_quantity", side_effect=pause_after_stock):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(complete)
            assert movement_written.wait(timeout=10)
            second = pool.submit(reset)
            blocked = False
            for _ in range(40):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query LIKE %s", ["%returns_stockreturn%"],
                    )
                    blocked = cursor.fetchone()[0] > 0
                if blocked:
                    break
                time.sleep(0.05)
            release_completion.set()
            assert blocked
            assert first.result(timeout=15).status == StockReturn.Status.COMPLETED
            with pytest.raises(ValidationError):
                second.result(timeout=15)
    assert StockReturn.objects.get(pk=ret.pk).status == StockReturn.Status.COMPLETED
    assert _movements(ret) == [Decimal("1")]
