"""Concurrency qualification for request-to-sale conversion on PostgreSQL 16."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection

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
