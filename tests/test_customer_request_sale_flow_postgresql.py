"""Concurrency qualification for request-to-sale conversion on PostgreSQL 16."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from apps.actions.models import PartCustomsInfo
from apps.customer_requests.sale_conversion import complete_request_sale, prepare_request_sale
from apps.customers.models import Customer
from apps.inventory.models import StockMovement
from apps.sales.models import Sale

from .test_customer_request_sale_flow import make_request, take

pytest_plugins = ["tests.test_customer_request_sale_flow"]

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL 16 concurrency qualification",
    ),
]


def _race(fn, count=2):
    barrier = Barrier(count)

    def run():
        close_old_connections()
        try:
            barrier.wait(20)
            return fn()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(lambda _unused: run(), range(count)))


def _post_customs(request_pk, admin_pk, part_pk):
    client = Client()
    client.force_login(get_user_model().objects.get(pk=admin_pk))
    return client.post(
        reverse("customer_request_customs", args=[request_pk]),
        {
            "metadata_submit": "1",
            "part_id": str(part_pk),
            f"gross_weight_g_{part_pk}": "180",
            f"net_weight_g_{part_pk}": "120",
            f"application_area_{part_pk}": "СНЕГОХОД",
        },
    )


def test_postgresql_request_prepare_and_complete_double_submit_is_single_sale(
    sale_scene,
):
    customer = Customer.objects.create(name="PG клиент", phone="+79090000001")
    request = take(
        make_request(sale_scene["part"], key="pg16-concurrent-request"),
        sale_scene["admin"],
    )

    prepared = _race(
        lambda: prepare_request_sale(request_id=request.pk, by=sale_scene["admin"])
    )
    sale_ids = {sale.pk for sale in prepared}
    assert len(sale_ids) == 1
    sale_id = sale_ids.pop()
    assert Sale.objects.filter(pk=sale_id).count() == 1
    assert StockMovement.objects.filter(document_type="sale").count() == 0

    completed = _race(
        lambda: complete_request_sale(
            request_id=request.pk, sale_id=sale_id, by=sale_scene["admin"]
        )
    )

    assert {sale.pk for sale in completed} == {sale_id}
    assert Sale.objects.filter(pk=sale_id, status=Sale.Status.COMPLETED).count() == 1
    assert StockMovement.objects.filter(document_type="sale").count() == 1
    request.refresh_from_db()
    assert request.status == request.Status.COMPLETED
    assert request.customer_id == customer.pk
    assert request.sale_id == sale_id


def test_postgresql_request_customs_two_operators_serialize_without_lost_update(
    sale_scene,
):
    request = take(
        make_request(sale_scene["part"], key="pg16-customs-concurrent"),
        sale_scene["admin"],
    )
    sale = prepare_request_sale(
        request_id=request.pk, by=sale_scene["admin"], create_customer=True
    )
    request_line = request.lines.get()
    request_snapshot = (
        request_line.part_name,
        request_line.article,
        request_line.quantity_requested,
        request_line.price_seen,
    )
    sale_line = sale.lines.get()
    sale_snapshot = (sale_line.quantity, sale_line.unit_price, sale.status)
    PartCustomsInfo.objects.filter(part_type=sale_scene["part"]).update(
        gross_weight_kg=None, net_weight_kg=None, application_area=""
    )

    responses = _race(
        lambda: _post_customs(
            request.pk, sale_scene["admin"].pk, sale_scene["part"].pk
        )
    )

    assert [response.status_code for response in responses] == [302, 302]
    customs = PartCustomsInfo.objects.get(part_type=sale_scene["part"])
    assert customs.gross_weight_kg == Decimal("0.180")
    assert customs.net_weight_kg == Decimal("0.120")
    assert customs.application_area == "СНЕГОХОД"
    assert PartCustomsInfo.objects.filter(part_type=sale_scene["part"]).count() == 1
    assert StockMovement.objects.filter(document_type="sale").count() == 0
    assert Sale.objects.filter(pk=sale.pk, status=Sale.Status.DRAFT).count() == 1

    request.refresh_from_db()
    sale.refresh_from_db()
    request_line.refresh_from_db()
    sale_line.refresh_from_db()
    assert request.status == request.Status.IN_PROGRESS
    assert request.sale_id == sale.pk
    assert (
        request_line.part_name,
        request_line.article,
        request_line.quantity_requested,
        request_line.price_seen,
    ) == request_snapshot
    assert (sale_line.quantity, sale_line.unit_price, sale.status) == sale_snapshot
