from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.customers.models import Customer, CustomerLifecycleEvent
from apps.customers.services import CustomerArchiveError, archive_customer
from apps.sales.models import Sale
from apps.sales.services import SaleError, create_sale

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"),
]


@pytest.fixture
def archive_race_scene():
    user_model = get_user_model()
    first = user_model.objects.create_superuser("archive-race-first", password="test")
    second = user_model.objects.create_superuser("archive-race-second", password="test")
    customer = Customer.objects.create(name="Клиент для гонки архивации")
    return first, second, customer


def test_two_operators_racing_archive_create_one_audit_event(archive_race_scene):
    first, second, customer = archive_race_scene
    barrier = Barrier(2)

    def archive(user):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            try:
                archive_customer(customer_id=customer.pk, by=user, reason="Параллельное действие")
            except CustomerArchiveError:
                return "already_archived"
            return "archived"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(archive, (first, second)))

    assert sorted(results) == ["already_archived", "archived"]
    assert CustomerLifecycleEvent.objects.filter(customer=customer).count() == 1
    assert Customer.objects.get(pk=customer.pk).is_archived


def test_archive_racing_new_sale_serializes_customer_use(archive_race_scene):
    operator, _second, customer = archive_race_scene
    barrier = Barrier(2)

    def archive():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            archive_customer(customer_id=customer.pk, by=operator, reason="Обновление карточки")
            return "archived"
        finally:
            close_old_connections()

    def create_draft():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            try:
                sale = create_sale(customer=Customer.objects.get(pk=customer.pk), by=operator)
            except SaleError:
                return "refused"
            return sale.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        archive_future = pool.submit(archive)
        sale_future = pool.submit(create_draft)
        assert archive_future.result() == "archived"
        sale_result = sale_future.result()

    customer.refresh_from_db()
    if sale_result == "refused":
        assert not Sale.objects.filter(customer=customer).exists()
    else:
        # The sale acquired the row lock first and completed before archiving.
        assert Sale.objects.get(pk=sale_result).customer_id == customer.pk
    assert customer.is_archived
