"""Round 2: distinguish return postings from old sale-cancellation ID collisions."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.urls import reverse

from apps.inventory.models import StockMovement
from apps.returns.models import StockReturn, StockReturnLine
from apps.returns.movement_attribution import return_movement_evidence
from apps.returns.services import (
    ReturnError,
    add_repair_line_return,
    add_sale_line_return,
    cancel_return,
    complete_return,
    create_return,
)
from apps.sales.services import cancel_sale

pytest_plugins = ("tests.test_returns",)


def _draft(data, *, source=None):
    ret = create_return(source=source or data["sale"], by=data["admin"])
    if source is data["order"]:
        add_repair_line_return(
            ret, data["repair_lot_line"], Decimal("1"), to_location=data["loc"],
            restock_status=StockReturnLine.RestockStatus.AVAILABLE, by=data["admin"],
        )
    else:
        add_sale_line_return(
            ret, data["sale_lot_line"], Decimal("1"), to_location=data["loc2"],
            restock_status=StockReturnLine.RestockStatus.AVAILABLE, by=data["admin"],
        )
    return ret


def _return_movements(ret):
    return StockMovement.objects.filter(
        document_type="stock_return", document_id=ret.pk,
        movement_type=StockMovement.MovementType.RETURN_LOT,
    )


def _old_sale_collision(data, *, with_line=True):
    # The current sale service correctly writes document_type=sale. Re-label one
    # already posted movement locally to reproduce the historical writer.
    sale = cancel_sale(data["sale"], by=data["admin"], reason="Неверный номер", author="Тест")
    legacy = StockMovement.objects.get(
        movement_type=StockMovement.MovementType.RETURN_LOT,
        document_type="sale", document_id=sale.pk, stock_lot=data["lot_small"],
    )
    StockMovement.objects.filter(pk=legacy.pk).update(document_type="stock_return")
    ret = create_return(source=data["order"], by=data["admin"])
    if ret.pk != sale.pk:
        assert not StockReturn.objects.filter(pk=sale.pk).exists()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE returns_stockreturn SET id=%s WHERE id=%s", [sale.pk, ret.pk])
        ret.pk = sale.pk
    if with_line:
        add_repair_line_return(
            ret, data["repair_lot_line"], Decimal("1"), to_location=data["loc"],
            restock_status=StockReturnLine.RestockStatus.AVAILABLE, by=data["admin"],
        )
    assert ret.pk == sale.pk
    assert legacy.created_at < ret.created_at
    return ret, legacy


@pytest.mark.django_db
def test_old_cancelled_sale_collision_does_not_block_valid_completion(data):
    ret, legacy = _old_sale_collision(data)
    evidence = return_movement_evidence(ret)
    assert evidence.unrelated_ids == (legacy.pk,)
    assert not evidence.possible_posting
    complete_return(ret, by=data["admin"])
    evidence = return_movement_evidence(ret)
    assert evidence.unrelated_ids == (legacy.pk,)
    assert len(evidence.owned_ids) == 1
    assert evidence.ambiguous_ids == ()
    assert _return_movements(ret).count() == 2  # two different business events
    assert ret.lines.get().returned_lot_id == data["lot"].pk


@pytest.mark.django_db
def test_unrelated_collision_does_not_block_draft_deletion(data, client):
    ret, legacy = _old_sale_collision(data, with_line=False)
    assert return_movement_evidence(ret).unrelated_ids == (legacy.pk,)
    client.force_login(data["admin"])
    response = client.post(reverse("admin:returns_stockreturn_delete", args=[ret.pk]),
                           {"post": "yes"})
    assert response.status_code == 302
    assert not StockReturn.objects.filter(pk=ret.pk).exists()
    assert StockMovement.objects.filter(pk=legacy.pk).exists()


@pytest.mark.django_db
def test_unrelated_collision_allows_draft_line_and_bulk_admin_deletion(data, client):
    ret, legacy = _old_sale_collision(data)
    line = ret.lines.get()
    StockReturnLine.objects.filter(pk=line.pk).delete()
    assert not ret.lines.exists()
    client.force_login(data["admin"])
    response = client.post(reverse("admin:returns_stockreturn_changelist"), {
        "action": "delete_selected", "_selected_action": [ret.pk], "post": "yes",
    })
    assert response.status_code == 302
    assert not StockReturn.objects.filter(pk=ret.pk).exists()
    assert StockMovement.objects.filter(pk=legacy.pk).exists()


@pytest.mark.django_db
def test_damaged_sale_cancellation_proof_remains_ambiguous(data):
    ret, legacy = _old_sale_collision(data, with_line=False)
    original = StockMovement.objects.get(
        document_type="sale", document_id=ret.pk,
        movement_type=StockMovement.MovementType.SALE_LOT,
        stock_lot=data["lot_small"],
    )
    StockMovement.objects.filter(pk=original.pk).update(document_type="damaged")
    assert return_movement_evidence(ret).ambiguous_ids == (legacy.pk,)
    with pytest.raises(ValidationError, match="неоднозначна"):
        StockReturn.objects.filter(pk=ret.pk).delete()


def _posted_draft(data):
    ret = _draft(data)
    complete_return(ret, by=data["admin"])
    with connection.cursor() as cursor:
        cursor.execute("UPDATE returns_stockreturn SET status='draft' WHERE id=%s", [ret.pk])
    return ret


@pytest.mark.django_db
def test_real_posting_protects_draft_model_queryset_and_line_deletion(data):
    ret = _posted_draft(data)
    line = ret.lines.get()
    assert len(return_movement_evidence(ret).owned_ids) == 1
    for delete in (
        lambda: StockReturn.objects.get(pk=ret.pk).delete(),
        lambda: StockReturn.objects.filter(pk=ret.pk).delete(),
        lambda: StockReturnLine.objects.get(pk=line.pk).delete(),
        lambda: StockReturnLine.objects.filter(pk=line.pk).delete(),
    ):
        with pytest.raises(ValidationError):
            delete()
    assert StockReturn.objects.filter(pk=ret.pk).exists()
    assert StockReturnLine.objects.filter(pk=line.pk).exists()
    assert _return_movements(ret).count() == 1


@pytest.mark.django_db
def test_real_posting_protects_draft_admin_object_and_bulk_deletion(data, client):
    ret = _posted_draft(data)
    client.force_login(data["admin"])
    direct = client.post(reverse("admin:returns_stockreturn_delete", args=[ret.pk]),
                         {"post": "yes"})
    assert direct.status_code == 403
    bulk = client.post(reverse("admin:returns_stockreturn_changelist"), {
        "action": "delete_selected", "_selected_action": [ret.pk], "post": "yes",
    })
    assert bulk.status_code == 403
    assert StockReturn.objects.filter(pk=ret.pk).exists()
    assert _return_movements(ret).count() == 1


@pytest.mark.django_db
def test_real_posting_protects_draft_admin_inline_deletion(data, client):
    ret = _posted_draft(data)
    line = ret.lines.get()
    client.force_login(data["admin"])
    response = client.post(reverse("admin:returns_stockreturn_change", args=[ret.pk]), {
        "reason": "Попытка изменения", "comment": "Попытка изменения",
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-id": str(line.pk), "lines-0-stock_return": str(ret.pk),
        "lines-0-DELETE": "on",
    })
    assert response.status_code in (200, 302)
    assert StockReturnLine.objects.filter(pk=line.pk).exists()
    assert _return_movements(ret).count() == 1


@pytest.mark.django_db
def test_ambiguous_history_fails_closed_without_inventory_write(data, client):
    ret = _draft(data)
    other = data["lot_small"]
    ambiguous = StockMovement.objects.create(
        movement_type=StockMovement.MovementType.RETURN_LOT,
        part_type=other.part_type, stock_lot=other, batch=other.batch,
        batch_line=other.batch_line, to_location=other.location,
        quantity=Decimal("1"), document_type="stock_return", document_id=ret.pk,
        comment="Историческое поступление без доказанного источника",
    )
    StockMovement.objects.filter(pk=ambiguous.pk).update(
        created_at=ret.created_at - timedelta(days=2)
    )
    assert return_movement_evidence(ret).ambiguous_ids == (ambiguous.pk,)
    with pytest.raises(ReturnError, match="неоднозначна"):
        complete_return(ret, by=data["admin"])
    with pytest.raises(ValidationError, match="неоднозначна"):
        StockReturn.objects.filter(pk=ret.pk).delete()
    with pytest.raises(ValidationError, match="неоднозначна"):
        ret.lines.get().delete()
    client.force_login(data["admin"])
    assert client.post(reverse("admin:returns_stockreturn_delete", args=[ret.pk]),
                       {"post": "yes"}).status_code == 403
    assert ret.lines.get().returned_lot_id is None
    assert StockReturn.objects.get(pk=ret.pk).status == StockReturn.Status.DRAFT


@pytest.mark.django_db
def test_completed_reason_comment_immutable_through_orm_and_admin(data, client):
    ret = _draft(data)
    complete_return(ret, by=data["admin"])
    for field in ("reason", "comment"):
        setattr(ret, field, f"Model {field}")
        with pytest.raises(ValidationError):
            ret.save(update_fields=[field])
        with pytest.raises(ValidationError):
            StockReturn.objects.filter(pk=ret.pk).update(**{field: f"Query {field}"})
        with pytest.raises(ValidationError):
            with transaction.atomic():
                StockReturn.objects.bulk_update([ret], [field])
        ret.refresh_from_db()
    client.force_login(data["admin"])
    line = ret.lines.get()
    response = client.post(reverse("admin:returns_stockreturn_change", args=[ret.pk]), {
        "reason": "Admin reason", "comment": "Admin comment",
        "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-id": str(line.pk), "lines-0-stock_return": str(ret.pk),
    })
    assert response.status_code == 302
    ret.refresh_from_db()
    assert (ret.reason, ret.comment) == ("", "")
    assert _return_movements(ret).count() == 1


@pytest.mark.django_db
def test_genuine_draft_metadata_still_editable_and_cancellable(data):
    ret = _draft(data)
    ret.reason = "Обычная причина"
    ret.save(update_fields=["reason"])
    StockReturn.objects.filter(pk=ret.pk).update(comment="Обычный комментарий")
    ret.refresh_from_db()
    assert (ret.reason, ret.comment) == ("Обычная причина", "Обычный комментарий")
    cancel_return(ret, by=data["admin"], reason="Черновик закрыт")
    assert StockReturn.objects.get(pk=ret.pk).status == StockReturn.Status.CANCELED


@pytest.mark.django_db
def test_posted_draft_cannot_cancel_without_compensating_stock(data):
    ret = _posted_draft(data)
    with pytest.raises(ReturnError, match="требует проверки"):
        cancel_return(ret, by=data["admin"], reason="Нельзя потерять движение")
    assert StockReturn.objects.get(pk=ret.pk).status == StockReturn.Status.DRAFT
    assert _return_movements(ret).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("contender", ["header_delete", "line_update", "line_delete"])
def test_completion_and_mutation_wait_for_same_postgresql_lock(data, contender):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    ret = _draft(data)
    line = ret.lines.get()
    from apps.returns import services

    original = services.return_stock_lot_quantity
    inside = threading.Event()
    release = threading.Event()

    def pause_after_movement(*args, **kwargs):
        result = original(*args, **kwargs)
        inside.set()
        assert release.wait(timeout=15)
        return result

    def worker_complete():
        close_old_connections()
        try:
            return complete_return(StockReturn(pk=ret.pk), by=data["admin"])
        finally:
            close_old_connections()

    def worker_mutation():
        close_old_connections()
        try:
            if contender == "line_update":
                return StockReturnLine.objects.filter(pk=line.pk).update(quantity=2)
            if contender == "line_delete":
                return StockReturnLine.objects.filter(pk=line.pk).delete()
            return StockReturn.objects.filter(pk=ret.pk).delete()
        finally:
            close_old_connections()

    with patch.object(services, "return_stock_lot_quantity", side_effect=pause_after_movement):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker_complete)
            assert inside.wait(timeout=15)
            second = pool.submit(worker_mutation)
            blocked = False
            for _ in range(100):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND query ILIKE %s",
                        ["%returns_stockreturn%"],
                    )
                    blocked = cursor.fetchone()[0] > 0
                if blocked:
                    break
                time.sleep(0.05)
            release.set()
            assert blocked, f"{contender} never waited for the return header lock"
            assert first.result(timeout=15).status == StockReturn.Status.COMPLETED
            with pytest.raises(ValidationError):
                second.result(timeout=15)
    assert StockReturn.objects.get(pk=ret.pk).status == StockReturn.Status.COMPLETED
    assert StockReturnLine.objects.get(pk=line.pk).quantity == Decimal("1")
    assert _return_movements(ret).count() == 1
