"""Stage 9 contract tests for customer requests and their operator workflow."""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.customer_requests.models import (
    CustomerRequest,
    CustomerRequestPrivacyEvent,
    CustomerRequestStatusEvent,
)
from apps.customer_requests.services import (
    CustomerRequestError,
    RequestLineInput,
    anonymize_request,
    change_request_status,
    create_customer_request,
    withdraw_consent,
)
from apps.inventory.availability import available_totals
from apps.inventory.models import StockBalance, StockMovement
from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, ReservationLine, Sale, SaleLine

PASSWORD = "parol-12345"
POLICY = "draft-legal-review-1"


@pytest.fixture
def part(db):
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    unit = Unit.objects.get(name="Штука")
    manufacturer, _ = Manufacturer.objects.get_or_create(name="BRP")
    result = PartType.objects.create(
        name="РЕМЕНЬ ПРИВОДНОЙ",
        category=category,
        unit=unit,
        manufacturer=manufacturer,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("10000.00"),
    )
    PartNumber.objects.create(part=result, value="448", is_primary=True)
    return result


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="boss", password=PASSWORD)


def _create(*, part, key="a" * 32, supply=True, quantity="2"):
    return create_customer_request(
        customer_name="Иван Петров",
        customer_phone="+7 (912) 123-45-67",
        preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
        comment="Нужна деталь.",
        lines=[RequestLineInput(part_id=part.pk, quantity=quantity, supply_inquiry=supply)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=key,
    )


def test_supply_request_is_a_price_snapshot_without_stock_side_effects(part):
    before = {
        "balances": StockBalance.objects.count(),
        "movements": StockMovement.objects.count(),
        "reservations": Reservation.objects.count(),
        "reservation_lines": ReservationLine.objects.count(),
        "sales": Sale.objects.count(),
        "sale_lines": SaleLine.objects.count(),
        "repairs": RepairOrder.objects.count(),
        "available": available_totals([part.pk]),
    }

    request, created = _create(part=part)

    line = request.lines.get()
    assert created is True
    assert line.is_supply_inquiry is True
    assert line.price_seen == Decimal("10000.00")
    assert line.article == "448"
    assert line.quantity_requested == Decimal("2")
    assert CustomerRequest.objects.count() == 1
    assert {
        "balances": StockBalance.objects.count(),
        "movements": StockMovement.objects.count(),
        "reservations": Reservation.objects.count(),
        "reservation_lines": ReservationLine.objects.count(),
        "sales": Sale.objects.count(),
        "sale_lines": SaleLine.objects.count(),
        "repairs": RepairOrder.objects.count(),
        "available": available_totals([part.pk]),
    } == before


def test_normal_request_rechecks_current_availability(part):
    with pytest.raises(CustomerRequestError, match="Сейчас доступно"):
        _create(part=part, supply=False)
    assert CustomerRequest.objects.count() == 0


def test_supply_request_requires_zero_stock(part, monkeypatch):
    monkeypatch.setattr(
        "apps.customer_requests.services.available_totals",
        lambda _ids: {part.pk: Decimal("1")},
    )

    with pytest.raises(CustomerRequestError, match="поставке"):
        _create(part=part)


def test_idempotency_returns_one_original_request(part):
    first, created = _create(part=part, key="b" * 32)
    second, repeated = _create(part=part, key="b" * 32)

    assert created is True
    assert repeated is False
    assert second.pk == first.pk
    assert CustomerRequest.objects.count() == 1


def test_request_submission_query_count_is_bounded_for_one_twenty_and_fifty_lines(part):
    """Part identity, current pricing and availability are hydrated in batches."""
    parts = [part]
    for index in range(2, 51):
        parts.append(
            PartType.objects.create(
                name=f"Запросная деталь {index}",
                category=part.category,
                unit=part.unit,
                manufacturer=part.manufacturer,
                tracking_mode=PartType.TrackingMode.BULK,
                recommended_price=Decimal("10000.00"),
            )
        )
    query_counts = []
    for line_count in (1, 20, 50):
        with CaptureQueriesContext(connection) as captured:
            create_customer_request(
                customer_name="Иван Петров",
                customer_phone="+7 (912) 123-45-67",
                preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
                lines=[
                    RequestLineInput(part_id=item.pk, quantity="1", supply_inquiry=True)
                    for item in parts[:line_count]
                ],
                privacy_policy_version=POLICY,
                personal_data_consent_version=POLICY,
                submission_key=str(line_count) * 32,
            )
        query_counts.append(len(captured))

    assert max(query_counts) - min(query_counts) <= 2


def test_consent_versions_and_phone_search_snapshot_are_kept(part):
    request, _ = _create(part=part)

    request.refresh_from_db()
    assert request.privacy_policy_version == POLICY
    assert request.personal_data_consent_version == POLICY
    assert request.consent_accepted_at is not None
    assert request.customer_phone_normalized == "79121234567"


def test_withdrawal_and_explicit_anonymization_minimize_only_personal_data(part, admin):
    request, _ = _create(part=part)
    original_line = request.lines.get()

    with pytest.raises(CustomerRequestError, match="Сначала"):
        anonymize_request(request_id=request.pk, by=admin)
    withdraw_consent(request_id=request.pk, by=admin)
    anonymized = anonymize_request(request_id=request.pk, by=admin)

    assert anonymized.customer_name == ""
    assert anonymized.customer_phone == ""
    assert anonymized.customer_phone_normalized == ""
    assert anonymized.comment == ""
    assert anonymized.data_anonymized_at is not None
    assert anonymized.lines.get().pk == original_line.pk
    assert list(
        CustomerRequestPrivacyEvent.objects.order_by("pk").values_list("event_type", flat=True)
    ) == ["consent_withdrawn", "anonymized"]


def test_status_workflow_is_audited_and_retry_is_idempotent(part, admin):
    request, _ = _create(part=part)
    before_available = available_totals([part.pk])
    changed, changed_once = change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.IN_PROGRESS, by=admin
    )
    retried, changed_twice = change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.IN_PROGRESS, by=admin
    )
    completed, _ = change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.COMPLETED, by=admin
    )

    assert changed_once is True
    assert changed_twice is False
    assert changed.pk == retried.pk == completed.pk
    assert completed.status == CustomerRequest.Status.COMPLETED
    assert list(
        CustomerRequestStatusEvent.objects.order_by("pk").values_list("from_status", "to_status")
    ) == [
        ("new", "in_progress"),
        ("in_progress", "completed"),
    ]
    assert available_totals([part.pk]) == before_available
    with pytest.raises(CustomerRequestError, match="недоступен"):
        change_request_status(
            request_id=request.pk, target_status=CustomerRequest.Status.NEW, by=admin
        )


def test_operator_screens_permission_badge_and_status_post(client, part, admin, django_user_model):
    customer_request, _ = _create(part=part)
    viewer = django_user_model.objects.create_user(username="viewer", password=PASSWORD)
    viewer.groups.add(Group.objects.get(name=roles.VIEWER))
    client.login(username="viewer", password=PASSWORD)
    assert client.get(reverse("customer_request_list")).status_code == 403

    client.login(username="boss", password=PASSWORD)
    with CaptureQueriesContext(connection) as list_queries:
        response = client.get(reverse("customer_request_list"))
    assert response.status_code == 200
    assert "Заявки клиентов" in response.content.decode()
    assert "1" in response.content.decode()
    assert len(list_queries) <= 8
    with CaptureQueriesContext(connection) as detail_queries:
        response = client.get(reverse("customer_request_detail", args=[customer_request.pk]))
    assert response.status_code == 200
    assert "РЕМЕНЬ ПРИВОДНОЙ" in response.content.decode()
    assert len(detail_queries) <= 14
    response = client.post(
        reverse("customer_request_status", args=[customer_request.pk]), {"status": "in_progress"}
    )
    assert response.status_code == 302
    customer_request.refresh_from_db()
    assert customer_request.status == CustomerRequest.Status.IN_PROGRESS


def test_a_refused_status_change_returns_to_the_request_with_the_reason(client, part, admin):
    """A stale page offering an old action must not fail with a server error."""
    customer_request, _ = _create(part=part)
    change_request_status(request_id=customer_request.pk, target_status="in_progress", by=admin)
    change_request_status(request_id=customer_request.pk, target_status="completed", by=admin)
    client.login(username="boss", password=PASSWORD)

    response = client.post(
        reverse("customer_request_status", args=[customer_request.pk]),
        {"status": "canceled"},
        follow=True,
    )

    assert response.redirect_chain[-1] == (
        reverse("customer_request_detail", args=[customer_request.pk]),
        302,
    )
    assert "Этот переход статуса недоступен." in response.content.decode()
    customer_request.refresh_from_db()
    assert customer_request.status == CustomerRequest.Status.COMPLETED
    missing = client.post(reverse("customer_request_status", args=[999999]), {"status": "canceled"})
    assert missing.status_code == 404
