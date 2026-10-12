from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.counting.cell_state import cell_state
from apps.counting.models import InventoryCountingSession, InventoryScanEvent
from apps.counting.services import (
    CountingError,
    cancel_session,
    convert_to_receipt,
    post_session,
    record_scan,
    restart_session,
    start_session,
)
from apps.inventory.models import StockBalance, StockMovement
from apps.receipts.models import Receipt
from apps.receipts.services import ReceiptError, add_line, create_receipt, post_receipt
from apps.stocktaking.models import SectionRecount
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location


@pytest.fixture
def scene(db):
    user = get_user_model().objects.create_superuser("count-cancel-admin", password="test")
    category = Category.objects.create(name="Counting cancellation")
    part = PartType.objects.create(
        name="Seal for cancellation test",
        category=category,
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value="P0-CANCEL-1", kind=PartNumber.Kind.OEM)
    location = get_or_create_location("S01-D03-C08", name="Cancellation cell")
    supplier = Supplier.objects.create(name="Cancellation test supplier", is_active=True)
    return {"user": user, "part": part, "location": location, "supplier": supplier}


def receive(scene, quantity):
    receipt = create_receipt(supplier=scene["supplier"], by=scene["user"])
    add_line(
        receipt,
        part_type=scene["part"],
        quantity=Decimal(quantity),
        unit_cost_rub=Decimal("25"),
        location=scene["location"],
    )
    return post_receipt(receipt, by=scene["user"])


def test_cancel_keeps_scan_audit_and_stock_unchanged(scene):
    session = start_session(location=scene["location"], by=scene["user"])
    record_scan(session, "P0-CANCEL-1", by=scene["user"])
    before = (StockBalance.objects.count(), StockMovement.objects.count())

    cancel_session(session, by=scene["user"], reason="Подсчёт устарел")

    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.CANCELLED
    assert session.cancelled_by_id == scene["user"].pk
    assert session.cancellation_reason == "Подсчёт устарел"
    assert session.cancelled_at is not None
    assert session.lines.count() == 1
    assert InventoryScanEvent.objects.filter(session=session).count() == 1
    assert (StockBalance.objects.count(), StockMovement.objects.count()) == before
    with pytest.raises(CountingError):
        post_session(session, by=scene["user"])


def test_restart_is_atomic_idempotent_and_uses_current_cell_baseline(scene):
    old = start_session(location=scene["location"], by=scene["user"])
    other_location = get_or_create_location("S02-D03-C08", name="Other cell")
    unrelated = start_session(location=other_location, by=scene["user"])
    record_scan(old, "P0-CANCEL-1", by=scene["user"])
    old_receipt = convert_to_receipt(old, by=scene["user"])
    receive(scene, 14)
    before = (StockBalance.objects.count(), StockMovement.objects.count())

    with pytest.raises(CountingError, match="менялись"):
        post_session(old, by=scene["user"])

    replacement = restart_session(old, by=scene["user"], reason="После движения пересчитать")
    old.refresh_from_db()
    old_receipt.refresh_from_db()
    assert old.status == InventoryCountingSession.Status.CANCELLED
    assert old.replacement_recount_id == replacement.pk
    assert old_receipt.status == Receipt.Status.CANCELLED
    assert old.cancelled_by_id == scene["user"].pk
    assert old.cancellation_reason == "После движения пересчитать"
    assert replacement.restart_reason == "После движения пересчитать"
    assert replacement.scope == SectionRecount.Scope.CELL
    assert replacement.status == SectionRecount.Status.COUNTING
    assert sum(Decimal(row["quantity"]) for row in replacement.snapshot["lots"]) == Decimal("14")
    with pytest.raises(ReceiptError, match="отменено"):
        post_receipt(old_receipt, by=scene["user"])
    assert (StockBalance.objects.count(), StockMovement.objects.count()) == before
    state = cell_state(scene["location"], since=replacement.started_at)
    assert state["quantity"] == Decimal("14")
    assert state["changed"] is False
    unrelated.refresh_from_db()
    assert unrelated.status == InventoryCountingSession.Status.DRAFT

    repeated = restart_session(old, by=scene["user"], reason="Повторный запрос")
    assert repeated.pk == replacement.pk
    assert replacement.started_at >= old.cancelled_at
    assert SectionRecount.objects.filter(
        section_code=scene["location"].code,
        status__in=["draft", "counting", "ready", "applying"],
    ).count() == 1


def test_cancel_rejects_empty_reason_and_posted_or_repeated_requests(scene):
    session = start_session(location=scene["location"], by=scene["user"])
    with pytest.raises(CountingError, match="причин"):
        cancel_session(session, by=scene["user"], reason="  ")

    record_scan(session, "P0-CANCEL-1", by=scene["user"])
    post_session(session, by=scene["user"])
    with pytest.raises(CountingError, match="Проведённую"):
        cancel_session(session, by=scene["user"], reason="Поздно")

    cancelled = start_session(location=scene["location"], by=scene["user"])
    cancel_session(cancelled, by=scene["user"], reason="Проверка повтора")
    with pytest.raises(CountingError, match="уже отменена"):
        cancel_session(cancelled, by=scene["user"], reason="Повтор")


def test_cancel_confirmation_requires_post_reason_and_csrf(scene):
    client = Client(enforce_csrf_checks=True)
    client.force_login(scene["user"])
    session = start_session(location=scene["location"], by=scene["user"])
    url = reverse("counting_cancel", args=[session.pk])

    page = client.get(url, {"restart": "1"})
    assert page.status_code == 200
    assert "Подтвердить отмену" in page.content.decode()
    assert client.post(url, {"reason": "CSRF"}).status_code == 403

    page = client.get(url)
    client.cookies["csrftoken"] = page.cookies["csrftoken"]
    client.post(
        url,
        {"reason": "Нет отметки", "restart": "1"},
        HTTP_X_CSRFTOKEN=page.cookies["csrftoken"].value,
    )
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.DRAFT

    response = client.post(
        url,
        {"reason": "Проверка отмены", "confirm_cancel": "yes"},
        HTTP_X_CSRFTOKEN=page.cookies["csrftoken"].value,
    )
    assert response.status_code == 302
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.CANCELLED
    assert session.cancelled_by_id == scene["user"].pk
    assert session.cancellation_reason == "Проверка отмены"


def test_stale_post_offers_restart_and_non_inventory_user_is_denied(scene):
    session = start_session(location=scene["location"], by=scene["user"])
    record_scan(session, "P0-CANCEL-1", by=scene["user"])
    convert_to_receipt(session, by=scene["user"])
    receive(scene, 14)

    client = Client()
    client.force_login(scene["user"])
    detail = client.get(reverse("counting_detail", args=[session.pk]))
    assert "Отменить и пересчитать заново" in detail.content.decode()
    response = client.post(reverse("counting_post", args=[session.pk]), follow=True)
    assert response.status_code == 200
    assert "Отменить и пересчитать заново" in response.content.decode()

    seller = get_user_model().objects.create_user("count-cancel-seller", password="test")
    denied = Client()
    denied.force_login(seller)
    assert denied.get(reverse("counting_cancel", args=[session.pk])).status_code == 403
    assert denied.post(
        reverse("counting_cancel", args=[session.pk]),
        {"reason": "Нет доступа", "confirm_cancel": "yes", "restart": "1"},
    ).status_code == 403


def test_restart_confirmation_starts_only_one_replacement_for_two_operators(scene):
    session = start_session(location=scene["location"], by=scene["user"])
    client_a = Client()
    client_b = Client()
    second_operator = get_user_model().objects.create_superuser(
        "count-cancel-second", password="test"
    )
    client_a.force_login(scene["user"])
    client_b.force_login(second_operator)
    url = reverse("counting_cancel", args=[session.pk])
    for client in (client_a, client_b):
        response = client.post(
            url,
            {
                "reason": "Начать пересчёт заново",
                "confirm_cancel": "yes",
                "restart": "1",
            },
        )
        assert response.status_code == 302
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.CANCELLED
    assert session.cancelled_by_id == scene["user"].pk
    assert session.cancellation_reason == "Начать пересчёт заново"
    assert session.replacement_recount_id
    assert SectionRecount.objects.filter(
        section_code=scene["location"].code,
        status__in=["draft", "counting", "ready", "applying"],
    ).count() == 1


def test_failed_replacement_creation_rolls_back_cancellation(scene, monkeypatch):
    old = start_session(location=scene["location"], by=scene["user"])
    record_scan(old, "P0-CANCEL-1", by=scene["user"])
    receipt = convert_to_receipt(old, by=scene["user"])

    def fail_start(**kwargs):
        raise CountingError("Synthetic replacement failure")

    monkeypatch.setattr("apps.counting.services.create_cell_recount", fail_start)
    with pytest.raises(CountingError, match="Synthetic"):
        restart_session(old, by=scene["user"], reason="Проверка отката")

    old.refresh_from_db()
    receipt.refresh_from_db()
    assert old.status == InventoryCountingSession.Status.CONVERTED
    assert old.cancelled_at is None
    assert old.replacement_recount_id is None
    assert receipt.status == Receipt.Status.DRAFT


def test_operator_can_restart_after_a_separate_cancellation(scene):
    old = start_session(location=scene["location"], by=scene["user"])
    cancel_session(old, by=scene["user"], reason="Подсчёт прерван")
    receive(scene, 14)
    before = (StockBalance.objects.count(), StockMovement.objects.count())

    client = Client()
    client.force_login(scene["user"])
    detail = client.get(reverse("counting_detail", args=[old.pk]))
    assert "Начать сверочный пересчёт этой ячейки" in detail.content.decode()
    url = reverse("counting_cancel", args=[old.pk])
    confirm = client.get(url, {"restart": "1"})
    assert confirm.status_code == 200
    assert "Старая сессия уже отменена" in confirm.content.decode()
    response = client.post(
        url,
        {"restart": "1", "reason": "Новый факт", "confirm_cancel": "yes"},
    )
    assert response.status_code == 302

    old.refresh_from_db()
    replacement = old.replacement_recount
    assert replacement.status == SectionRecount.Status.COUNTING
    assert replacement.restart_reason == "Новый факт"
    assert sum(Decimal(row["quantity"]) for row in replacement.snapshot["lots"]) == Decimal("14")
    assert (StockBalance.objects.count(), StockMovement.objects.count()) == before
