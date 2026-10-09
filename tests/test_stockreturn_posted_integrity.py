"""A completed StockReturn is an immutable posted document.

Regression for: a completed return could be switched back to «draft» through
Django admin and completed again, creating a second RETURN_* movement and
increasing stock a second time.
"""

import threading
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from django.db import IntegrityError, connection, connections, transaction
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import StockMovement
from apps.receipts.remediation import (
    apply_historical_lot_cost_remediation,
    plan_historical_lot_cost_remediation,
)
from apps.returns.models import (
    PostedStockReturnError,
    StockReturn,
    StockReturnLine,
    posted_return_cost_correction,
)
from apps.returns.services import (
    ReturnError,
    add_repair_line_return,
    add_sale_line_return,
    cancel_return,
    complete_return,
    create_return,
    update_return_line_restock_status,
)
from tests import test_historical_lot_cost_remediation, test_returns

# Reuse the return-domain fixtures (sale, repair order, lots, admin user).
admin = test_returns.admin
data = test_returns.data
make_user = test_returns.make_user
bad_lot = test_historical_lot_cost_remediation.bad_lot

pg_only = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="database trigger exists on PostgreSQL only",
)

RETURN_TYPES = (
    StockMovement.MovementType.RETURN_LOT,
    StockMovement.MovementType.RETURN_ITEM,
)


class _FormFields(HTMLParser):
    """Collect what a browser would submit for the admin change form."""

    def __init__(self):
        super().__init__()
        self.fields: dict[str, str] = {}
        self._select = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name")
        if tag == "input" and name and attrs.get("type") not in ("submit", "button", "file"):
            if attrs.get("type") in ("checkbox", "radio"):
                if "checked" in attrs:
                    self.fields[name] = attrs.get("value", "on")
            else:
                self.fields[name] = attrs.get("value", "")
        elif tag == "select" and name:
            self._select = name
            self.fields.setdefault(name, "")
        elif tag == "option" and self._select and "selected" in attrs:
            self.fields[self._select] = attrs.get("value", "")
        elif tag == "textarea" and name:
            self._textarea = name
            self.fields[name] = ""

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        if tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self.fields[self._textarea] += data


def admin_form(client, ret):
    url = reverse("admin:returns_stockreturn_change", args=[ret.pk])
    response = client.get(url)
    assert response.status_code == 200
    parser = _FormFields()
    parser.feed(response.content.decode())
    return url, parser.fields


def _return_movements(ret):
    return StockMovement.objects.filter(document_id=ret.pk, movement_type__in=RETURN_TYPES)


@pytest.fixture
def completed(data):
    """A completed sale return of 2 units into the existing lot."""
    data["lot"].refresh_from_db()
    before = data["lot"].quantity
    ret = create_return(source=data["sale"], by=data["admin"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    ret.refresh_from_db()
    data["lot"].refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED
    assert _return_movements(ret).count() == 1
    assert data["lot"].quantity == before + Decimal("2")
    return {**data, "ret": ret, "lot_before": before}


def test_admin_cannot_revert_completed_return_to_draft_and_double_post(completed, client):
    ret, lot = completed["ret"], completed["lot"]
    client.force_login(completed["admin"])
    url, fields = admin_form(client, ret)
    fields["status"] = StockReturn.Status.DRAFT
    fields["_save"] = "Сохранить"
    client.post(url, fields)
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED

    # Even if a caller still tries to complete again, nothing is posted twice.
    try:
        complete_return(ret, by=completed["admin"])
    except ReturnError:
        pass
    lot.refresh_from_db()
    assert _return_movements(ret).count() == 1
    assert lot.quantity == completed["lot_before"] + Decimal("2")


def _draft_with_line(data, qty="1"):
    ret = create_return(source=data["sale"], by=data["admin"])
    line = add_sale_line_return(
        ret, data["sale_lot_line"], Decimal(qty),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    return ret, line


def _assert_posted_once(completed):
    ret, lot = completed["ret"], completed["lot"]
    lot.refresh_from_db()
    assert _return_movements(ret).count() == 1
    assert lot.quantity == completed["lot_before"] + Decimal("2")


# --- Admin HTTP ---------------------------------------------------------------


def test_admin_forged_post_cannot_edit_lines_or_status_of_a_posted_return(completed, client):
    ret = completed["ret"]
    line = ret.lines.get()
    client.force_login(completed["admin"])
    url, fields = admin_form(client, ret)
    # Forge everything a hostile browser could send, including inline line data.
    fields.update({
        "status": "draft", "source_id": "999", "reason": "forged",
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1",
        "lines-0-id": str(line.pk), "lines-0-stock_return": str(ret.pk),
        "lines-0-quantity": "50", "lines-0-DELETE": "on", "_save": "1",
    })
    client.post(url, fields)
    ret.refresh_from_db()
    line.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED
    assert ret.source_id == completed["sale"].pk and ret.reason != "forged"
    assert line.quantity == Decimal("2")
    _assert_posted_once(completed)


def test_admin_cannot_delete_or_reopen_canceled_return(completed, client):
    cancel_return(completed["ret"], by=completed["admin"], reason="ошибка")
    ret = completed["ret"]
    client.force_login(completed["admin"])
    url, fields = admin_form(client, ret)
    fields.update({"status": "draft", "_save": "1"})
    client.post(url, fields)
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.CANCELED
    delete_url = reverse("admin:returns_stockreturn_delete", args=[ret.pk])
    assert client.post(delete_url, {"post": "yes"}).status_code == 403
    assert StockReturn.objects.filter(pk=ret.pk).exists()
    with pytest.raises(ReturnError):
        complete_return(ret, by=completed["admin"])
    completed["lot"].refresh_from_db()
    assert completed["lot"].quantity == completed["lot_before"]  # compensated exactly once


def test_admin_bulk_delete_action_refuses_posted_returns_cleanly(completed, client):
    ret = completed["ret"]
    client.force_login(completed["admin"])
    response = client.post(
        reverse("admin:returns_stockreturn_changelist"),
        {"action": "delete_selected", "_selected_action": [ret.pk], "post": "yes"},
    )
    # Django's per-object delete permission turns the bulk action into a 403.
    assert response.status_code == 403
    assert StockReturn.objects.filter(pk=ret.pk).exists()
    _assert_posted_once(completed)


def test_deleting_the_user_who_posted_a_return_still_works(completed, make_user):
    """ON DELETE SET NULL rewrites completed_by on a posted row: that stays allowed."""
    clerk = make_user("clerk-who-posted")
    ret, _ = completed["ret"], None
    draft, _line = _draft_with_line(completed, "1")
    complete_return(draft, by=clerk)
    clerk.delete()
    draft.refresh_from_db()
    assert draft.status == StockReturn.Status.COMPLETED and draft.completed_by_id is None
    assert ret.status == StockReturn.Status.COMPLETED


def test_admin_draft_editing_still_works(data, client):
    ret, line = _draft_with_line(data)
    client.force_login(data["admin"])
    url, fields = admin_form(client, ret)
    fields.update({"reason": "уточнено", "_save": "1"})
    client.post(url, fields)
    ret.refresh_from_db()
    assert ret.reason == "уточнено" and ret.status == StockReturn.Status.DRAFT


# --- ORM paths ----------------------------------------------------------------


def test_stale_instance_cannot_revert_or_rewrite_a_posted_return(completed):
    stale = StockReturn.objects.get(pk=completed["ret"].pk)
    stale.status = StockReturn.Status.DRAFT
    with pytest.raises(PostedStockReturnError):
        stale.save()
    stale = StockReturn.objects.get(pk=completed["ret"].pk)
    stale.comment = "rewrite"
    with pytest.raises(PostedStockReturnError):
        stale.save(update_fields=["comment"])
    _assert_posted_once(completed)


def test_instance_loaded_as_draft_cannot_overwrite_after_completion(data):
    ret, _ = _draft_with_line(data)
    stale = StockReturn.objects.get(pk=ret.pk)  # loaded while still a draft
    complete_return(ret, by=data["admin"])
    stale.reason = "late edit"
    with pytest.raises(PostedStockReturnError):
        stale.save()  # would otherwise write status='draft' back
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED


def test_queryset_and_bulk_paths_are_refused(completed):
    ret = completed["ret"]
    with pytest.raises(PostedStockReturnError):
        StockReturn.objects.filter(pk=ret.pk).update(status="draft")
    with pytest.raises(PostedStockReturnError):
        StockReturn.objects.filter(pk=ret.pk).update(comment="x")
    ret.status = "draft"
    with pytest.raises(PostedStockReturnError):
        StockReturn.objects.bulk_update([ret], ["status"])
    with pytest.raises(PostedStockReturnError):
        StockReturn.objects.filter(pk=ret.pk).delete()
    with pytest.raises(PostedStockReturnError):
        StockReturn.objects.bulk_create([StockReturn(
            source_type="sale", source_id=1, status=StockReturn.Status.COMPLETED)])
    with pytest.raises(PostedStockReturnError):
        StockReturn.objects.create(source_type="sale", source_id=1, status="completed")
    _assert_posted_once(completed)


def test_posted_lines_cannot_be_edited_added_moved_or_deleted(completed, data):
    ret = completed["ret"]
    line = ret.lines.get()
    line.quantity = Decimal("40")
    with pytest.raises(PostedStockReturnError):
        line.save()
    line.refresh_from_db()
    line.part_type_id = data["sale_item_line"].part_type_id
    with pytest.raises(PostedStockReturnError):
        line.save(update_fields=["part_type"])
    with pytest.raises(PostedStockReturnError):
        StockReturnLine.objects.filter(pk=line.pk).update(quantity=Decimal("40"))
    line.refresh_from_db()
    line.quantity = Decimal("40")
    with pytest.raises(PostedStockReturnError):
        StockReturnLine.objects.bulk_update([line], ["quantity"])
    with pytest.raises(PostedStockReturnError):
        line.delete()
    with pytest.raises(PostedStockReturnError):
        StockReturnLine.objects.filter(pk=line.pk).delete()
    draft, _ = _draft_with_line(data, "1")
    with pytest.raises(PostedStockReturnError):
        StockReturnLine.objects.filter(pk=line.pk).update(stock_return=draft)
    with pytest.raises(ReturnError):
        add_sale_line_return(ret, data["sale_lot_line"], Decimal("1"), to_location=data["loc"],
                             restock_status="available", by=data["admin"])
    with pytest.raises(ReturnError):
        update_return_line_restock_status(line, restock_status="quarantine", by=data["admin"])
    line.refresh_from_db()
    assert line.quantity == Decimal("2") and line.stock_return_id == ret.pk
    _assert_posted_once(completed)


def test_draft_line_editing_and_deletion_still_work(data):
    ret, line = _draft_with_line(data)
    line = update_return_line_restock_status(line, restock_status="quarantine", by=data["admin"])
    assert line.restock_status == "quarantine"
    line.delete()
    assert not ret.lines.exists()
    ret.delete()


# --- Idempotency ----------------------------------------------------------------


def test_completion_is_idempotent_and_returns_the_existing_result(completed):
    ret = completed["ret"]
    again = complete_return(ret, by=completed["admin"])
    assert again.pk == ret.pk and again.completed_at == ret.completed_at
    assert again.cost_total == ret.cost_total
    _assert_posted_once(completed)


def test_repeated_http_completion_posts_once(data, client):
    ret, _ = _draft_with_line(data, "2")
    client.force_login(data["admin"])
    for _ in range(3):
        assert client.post(reverse("return_complete", args=[ret.pk])).status_code == 302
    assert _return_movements(ret).count() == 1


def test_draft_carrying_return_movements_is_never_posted_again(data):
    """A historical anomaly (draft that already has stock movements) is refused."""
    ret, line = _draft_with_line(data)
    StockMovement.objects.create(
        movement_type=StockMovement.MovementType.RETURN_LOT, part_type=line.part_type,
        stock_lot=data["lot"], quantity=Decimal("1"), document_type="stock_return",
        document_id=ret.pk,
    )
    data["lot"].refresh_from_db()
    before = data["lot"].quantity
    with pytest.raises(ReturnError, match="повторное проведение"):
        complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == before
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.DRAFT


def test_older_movement_reusing_the_document_id_does_not_block_completion(data):
    """Production shape: RET-000003/RET-000011 share ids with July movements that
    predate them.  Those are not this document's postings."""
    ret, line = _draft_with_line(data, "2")
    legacy = StockMovement.objects.create(
        movement_type=StockMovement.MovementType.RETURN_LOT, part_type=line.part_type,
        stock_lot=data["lot"], quantity=Decimal("1"), document_type="stock_return",
        document_id=ret.pk,
    )
    StockMovement.objects.filter(pk=legacy.pk).update(
        created_at=ret.created_at - timezone.timedelta(days=60),
    )
    data["lot"].refresh_from_db()
    before = data["lot"].quantity
    complete_return(ret, by=data["admin"])
    ret.refresh_from_db()
    data["lot"].refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED
    assert data["lot"].quantity == before + Decimal("2")


# --- Supported exception: receipt-proven cost correction ---------------------


def test_cost_correction_scope_changes_only_cost_columns(completed):
    ret = completed["ret"]
    line = ret.lines.get()
    with transaction.atomic(), posted_return_cost_correction():
        line.unit_cost_rub = Decimal("1.00")
        line.total_cost_rub = Decimal("2.00")
        line.save(update_fields=["unit_cost_rub", "total_cost_rub"])
        ret.cost_total = Decimal("2.00")
        ret.save(update_fields=["cost_total", "updated_at"])
        line.quantity = Decimal("9")
        with pytest.raises(PostedStockReturnError):
            line.save(update_fields=["quantity"])
        with pytest.raises(PostedStockReturnError):
            StockReturn.objects.filter(pk=ret.pk).update(status="draft")
    line.refresh_from_db()
    assert line.unit_cost_rub == Decimal("1.00") and line.quantity == Decimal("2")
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED
    _assert_posted_once(completed)


def test_receipt_proven_remediation_still_corrects_a_posted_return(bad_lot):
    """The one supported mutation of posted history: cost only, never stock."""
    user, receipt_line, lot, orders = bad_lot
    orders[0].refresh_from_db()
    ret = create_return(source=orders[0], by=user)
    add_repair_line_return(
        ret, orders[0].lines.get(), Decimal("1"), to_location=lot.location,
        restock_status="available", by=user,
    )
    complete_return(ret, by=user)
    ret.refresh_from_db()
    line = ret.lines.get()
    assert line.unit_cost_rub == Decimal("0")
    plan = plan_historical_lot_cost_remediation(
        lot_id=lot.pk, receipt_line_id=receipt_line.pk, expected_old_cost="0", new_cost="160",
    )
    assert line.pk in plan.return_line_ids
    apply_historical_lot_cost_remediation(plan)
    line.refresh_from_db()
    ret.refresh_from_db()
    assert line.unit_cost_rub == Decimal("160.00") and line.quantity == Decimal("1")
    assert ret.cost_total == Decimal("160.00")
    assert ret.status == StockReturn.Status.COMPLETED
    assert _return_movements(ret).count() == 1


# --- Database trigger (below the ORM) ------------------------------------------


def _raw(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@pg_only
def test_database_refuses_raw_status_reversal_and_line_edits(completed):
    ret = completed["ret"]
    line = ret.lines.get()
    attempts = [
        ("UPDATE returns_stockreturn SET status='draft' WHERE id=%s", [ret.pk]),
        ("UPDATE returns_stockreturn SET source_id=source_id+1 WHERE id=%s", [ret.pk]),
        ("UPDATE returns_stockreturn SET status='canceled' WHERE id=%s", [ret.pk]),
        ("DELETE FROM returns_stockreturn WHERE id=%s", [ret.pk]),
        ("UPDATE returns_stockreturnline SET quantity=50 WHERE id=%s", [line.pk]),
        ("DELETE FROM returns_stockreturnline WHERE id=%s", [line.pk]),
        ("UPDATE returns_stockreturnline SET unit_cost_rub=0 WHERE id=%s", [line.pk]),
    ]
    for sql, params in attempts:
        with pytest.raises(IntegrityError):
            _raw(sql, params)
    # The ORM's bypass paths end at the same wall.
    with pytest.raises(IntegrityError), transaction.atomic():
        StockReturn._base_manager.filter(pk=ret.pk).update(status="draft")
    bypass = StockReturn.objects.get(pk=ret.pk)
    bypass.status = StockReturn.Status.DRAFT
    with pytest.raises(IntegrityError), transaction.atomic():
        bypass.save_base()  # skips the model's save() override entirely
    _assert_posted_once(completed)


@pg_only
def test_database_refuses_fake_completion_without_movements(data):
    ret, _ = _draft_with_line(data)
    with pytest.raises(IntegrityError):
        _raw("UPDATE returns_stockreturn SET status='completed' WHERE id=%s", [ret.pk])
    with pytest.raises(IntegrityError):
        _raw(
            "INSERT INTO returns_stockreturn (number, status, source_type, source_id, reason,"
            " comment, cost_total, created_at, updated_at, cancel_reason) VALUES"
            " ('X-1', 'completed', 'sale', 1, '', '', 0, now(), now(), '')"
        )


# --- PostgreSQL concurrency ----------------------------------------------------


def _run_concurrently(*functions):
    barrier = threading.Barrier(len(functions))
    results = [None] * len(functions)

    def runner(index, function):
        try:
            barrier.wait(timeout=10)
            results[index] = function()
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            results[index] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=runner, args=(i, f)) for i, f in enumerate(functions)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "deadlock or hang"
    return results


@pg_only
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_completions_post_exactly_once(data):
    ret, _line = _draft_with_line(data, "2")
    data["lot"].refresh_from_db()
    before = data["lot"].quantity
    results = _run_concurrently(
        lambda: complete_return(StockReturn.objects.get(pk=ret.pk), by=data["admin"]),
        lambda: complete_return(StockReturn.objects.get(pk=ret.pk), by=data["admin"]),
    )
    assert all(isinstance(r, StockReturn) for r in results), results
    data["lot"].refresh_from_db()
    assert _return_movements(ret).count() == 1
    assert data["lot"].quantity == before + Decimal("2")


@pg_only
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_line_edit_racing_completion_never_changes_the_posted_line(data):
    ret, line = _draft_with_line(data, "2")

    def edit():
        with transaction.atomic():
            fresh = StockReturnLine.objects.get(pk=line.pk)
            fresh.quantity = Decimal("3")
            fresh.save()
            return "edited"

    results = _run_concurrently(
        lambda: complete_return(StockReturn.objects.get(pk=ret.pk), by=data["admin"]),
        edit,
    )
    line.refresh_from_db()
    movement = _return_movements(ret).get()
    # Either the edit landed BEFORE posting (and was posted), or it was refused.
    assert movement.quantity == line.quantity
    assert _return_movements(ret).count() == 1
    if isinstance(results[1], Exception):
        assert isinstance(results[1], (PostedStockReturnError, IntegrityError))


@pg_only
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_raw_status_reversal_racing_completion_cannot_double_post(data):
    ret, _ = _draft_with_line(data, "2")

    def revert():
        with transaction.atomic():
            StockReturn._base_manager.filter(pk=ret.pk).update(status="draft")
        return "reverted"

    _run_concurrently(
        lambda: complete_return(StockReturn.objects.get(pk=ret.pk), by=data["admin"]),
        revert,
    )
    ret.refresh_from_db()
    if ret.status == StockReturn.Status.DRAFT:  # the revert can only win before posting
        complete_return(ret, by=data["admin"])
    assert _return_movements(ret).count() == 1
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED


@pg_only
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_completion_racing_cancel_leaves_a_consistent_ledger(data):
    ret, _ = _draft_with_line(data, "2")
    complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    after_post = data["lot"].quantity
    results = _run_concurrently(
        lambda: cancel_return(StockReturn.objects.get(pk=ret.pk), by=data["admin"], reason="r"),
        lambda: complete_return(StockReturn.objects.get(pk=ret.pk), by=data["admin"]),
    )
    ret.refresh_from_db()
    data["lot"].refresh_from_db()
    assert _return_movements(ret).count() == 1
    assert ret.status == StockReturn.Status.CANCELED
    assert data["lot"].quantity == after_post - Decimal("2")
    assert not isinstance(results[0], Exception)
