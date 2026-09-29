"""A customer request never silently drops the sale that was built from it.

Two gaps are closed here:
* a request with a posted (COMPLETED) sale could be cancelled, leaving a
  cancelled request next to a live money and stock document;
* a cancelled request with a linked sale could be hard-deleted (one by one or
  with "Удалить все отменённые"), removing the request, its lines and its
  conversation while the sale stayed behind without its origin.

Cancelling a request whose sale is still a DRAFT keeps its current behavior
(owner decision pending): the draft survives, but the request can no longer be
hard-deleted while it is linked.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.customer_requests.models import CustomerRequest
from apps.customer_requests.sale_conversion import complete_request_sale, prepare_request_sale
from apps.customer_requests.services import (
    CustomerRequestError,
    change_request_status,
    delete_all_cancelled_requests,
    delete_cancelled_request,
    reopen_customer_request,
)
from apps.customers.models import Customer
from apps.inventory.models import StockLot, StockMovement
from apps.sales.models import Sale
from apps.sales.services import cancel_sale
from tests import test_customer_request_sale_flow as flow

admin = flow.admin
sale_scene = flow.sale_scene
make_request = flow.make_request
take = flow.take

CANCELED = CustomerRequest.Status.CANCELED


def _cancel(request, admin):
    return change_request_status(request_id=request.pk, target_status=CANCELED, by=admin)


def _with_draft(scene, key):
    request = take(make_request(scene["part"], key=key), scene["admin"])
    sale = prepare_request_sale(request_id=request.pk, by=scene["admin"], create_customer=True)
    return request, sale


def _with_completed_sale(scene, key):
    Customer.objects.create(name="Покупатель", phone="+79090000001")
    request, sale = _with_draft(scene, key)
    sale = complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=scene["admin"])
    assert sale.status == Sale.Status.COMPLETED
    return request, sale


# --- Requests without a sale: unchanged -------------------------------------


def test_request_without_sale_cancels_and_deletes_as_before(sale_scene):
    request = make_request(sale_scene["part"], key="plain")

    _cancel(request, sale_scene["admin"])
    assert delete_cancelled_request(request_id=request.pk, by=sale_scene["admin"]) is True

    assert not CustomerRequest.objects.filter(pk=request.pk).exists()


# --- Completed sale: the request cannot be cancelled --------------------------


def test_request_with_completed_sale_cannot_be_cancelled(sale_scene):
    request, sale = _with_completed_sale(sale_scene, "done-cancel")
    stock = StockLot.objects.get(pk=sale_scene["lot"].pk).quantity

    with pytest.raises(CustomerRequestError, match="продажа по ней уже проведена"):
        _cancel(request, sale_scene["admin"])

    request.refresh_from_db()
    sale.refresh_from_db()
    assert request.status != CANCELED
    assert sale.status == Sale.Status.COMPLETED
    assert StockLot.objects.get(pk=sale_scene["lot"].pk).quantity == stock


def test_reopen_then_cancel_is_refused_too(sale_scene):
    request, _sale = _with_completed_sale(sale_scene, "done-reopen")
    change_request_status(
        request_id=request.pk,
        target_status=CustomerRequest.Status.COMPLETED,
        by=sale_scene["admin"],
    )
    reopen_customer_request(request_id=request.pk, by=sale_scene["admin"], reason="уточнить")

    with pytest.raises(CustomerRequestError):
        _cancel(request, sale_scene["admin"])


def test_cancelled_sale_lets_the_request_be_cancelled_but_not_deleted(sale_scene):
    request, sale = _with_completed_sale(sale_scene, "done-void")
    cancel_sale(sale, by=sale_scene["admin"], reason="клиент отказался", author="Оператор")

    _cancel(request, sale_scene["admin"])
    request.refresh_from_db()
    assert request.status == CANCELED

    with pytest.raises(CustomerRequestError, match="по ней создана продажа"):
        delete_cancelled_request(request_id=request.pk, by=sale_scene["admin"])
    assert CustomerRequest.objects.filter(pk=request.pk, sale=sale).exists()


def test_crafted_status_post_cannot_cancel_a_request_with_a_completed_sale(client, sale_scene):
    request, _sale = _with_completed_sale(sale_scene, "done-post")
    client.force_login(sale_scene["admin"])

    response = client.post(
        reverse("customer_request_status", args=[request.pk]), {"status": CANCELED}, follow=True
    )

    assert "продажа по ней уже проведена" in response.content.decode()
    request.refresh_from_db()
    assert request.status != CANCELED


# --- Draft sale: cancel unchanged, hard delete refused -----------------------


def test_request_with_draft_sale_still_cancels_and_keeps_the_draft(sale_scene):
    request, sale = _with_draft(sale_scene, "draft-cancel")

    _cancel(request, sale_scene["admin"])

    request.refresh_from_db()
    sale.refresh_from_db()
    assert request.status == CANCELED
    assert request.sale_id == sale.pk
    assert sale.status == Sale.Status.DRAFT


def test_cancelled_request_with_draft_sale_cannot_be_deleted(sale_scene):
    request, sale = _with_draft(sale_scene, "draft-delete")
    _cancel(request, sale_scene["admin"])

    with pytest.raises(CustomerRequestError, match="по ней создана продажа"):
        delete_cancelled_request(request_id=request.pk, by=sale_scene["admin"])

    assert CustomerRequest.objects.get(pk=request.pk).sale_id == sale.pk
    assert CustomerRequest.objects.get(pk=request.pk).lines.count() == 1


def test_crafted_delete_post_is_refused_for_a_linked_request(client, sale_scene):
    request, _sale = _with_draft(sale_scene, "draft-post")
    _cancel(request, sale_scene["admin"])
    client.force_login(sale_scene["admin"])

    response = client.post(reverse("customer_request_delete", args=[request.pk]), follow=True)

    assert "по ней создана продажа" in response.content.decode()
    assert CustomerRequest.objects.filter(pk=request.pk).exists()


# --- Delete all cancelled: skips linked requests -----------------------------


def test_delete_all_skips_requests_with_a_sale(sale_scene):
    plain = make_request(sale_scene["part"], key="bulk-plain", phone="89090000002")
    _cancel(plain, sale_scene["admin"])
    linked, sale = _with_draft(sale_scene, "bulk-linked")
    _cancel(linked, sale_scene["admin"])

    deleted = delete_all_cancelled_requests(by=sale_scene["admin"])

    assert deleted == 1
    assert not CustomerRequest.objects.filter(pk=plain.pk).exists()
    assert CustomerRequest.objects.get(pk=linked.pk).sale_id == sale.pk


def test_delete_all_view_reports_kept_linked_requests(client, sale_scene):
    linked, _sale = _with_draft(sale_scene, "bulk-view")
    _cancel(linked, sale_scene["admin"])
    client.force_login(sale_scene["admin"])

    body = client.post(reverse("customer_request_delete_all"), follow=True).content.decode()

    assert "Удалено отменённых заявок: 0." in body
    assert "Не удалено заявок с продажей: 1." in body
    assert CustomerRequest.objects.filter(pk=linked.pk).exists()
    # the per-row delete button is not offered for a linked request
    assert reverse("customer_request_delete", args=[linked.pk]) not in body


def test_request_to_sale_idempotency_and_stock_are_untouched(sale_scene):
    request, sale = _with_draft(sale_scene, "idem")
    before = StockMovement.objects.count()

    again = prepare_request_sale(request_id=request.pk, by=sale_scene["admin"])

    assert again.pk == sale.pk
    assert Sale.objects.filter(customer_request=request).count() == 1
    assert StockMovement.objects.count() == before
    assert StockLot.objects.get(pk=sale_scene["lot"].pk).quantity == Decimal("3")
