"""Customs article provenance for sales converted from customer requests."""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.actions.customs_history import canonical_customs_lines
from apps.actions.models import WarehouseAction
from apps.catalog.models import PartNumber, PartType
from apps.customer_requests.models import CustomerRequest
from apps.customer_requests.sale_conversion import complete_request_sale, prepare_request_sale
from apps.customer_requests.services import RequestLineInput, create_customer_request
from apps.customs_orders.models import CustomsOrder
from apps.customs_orders.services import (
    create_customs_order_from_boundary,
    customs_sources,
    eligible_customs_sources,
    selection_payload,
)
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Sale
from tests.test_customer_request_sale_flow import make_request, take

pytest_plugins = ["tests.test_customer_request_sale_flow"]


@pytest.mark.parametrize("messenger", ["max", "telegram"])
def test_completed_request_sale_uses_request_article_snapshot(sale_scene, client, messenger):
    request = make_request(sale_scene["part"], key=f"customs-{messenger}")
    request.preferred_messenger = messenger
    request.save(update_fields=["preferred_messenger"])
    request = take(request, sale_scene["admin"])
    original_article = request.lines.get().article
    sale = prepare_request_sale(
        request_id=request.pk, by=sale_scene["admin"], create_customer=True
    )
    complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=sale_scene["admin"])
    assert not WarehouseAction.objects.filter(sale=sale).exists()

    PartNumber.objects.filter(part=sale_scene["part"]).update(value="NEW-ARTICLE")
    sale_scene["lot"].refresh_from_db()
    before = (sale_scene["lot"].quantity, StockMovement.objects.count(), Sale.objects.count())
    lines = [row for row in canonical_customs_lines() if row["document_id"] == sale.pk]
    assert len(lines) == 1
    assert lines[0]["number"] == original_article == "REQ-1"
    assert lines[0]["quantity"] == Decimal("1")
    found = [row["number"] for row in customs_sources() if row["document_number"] == sale.number]
    assert found == [
        original_article
    ]

    client.force_login(sale_scene["admin"])
    response = client.get(reverse("customs_order_selection"), {"order_type": "analog"})
    assert response.status_code == 200
    assert original_article in response.content.decode()
    sale_scene["lot"].refresh_from_db()
    after = (sale_scene["lot"].quantity, StockMovement.objects.count(), Sale.objects.count())
    assert after == before


def test_missing_request_article_is_not_invented_from_current_card(sale_scene):
    request = make_request(sale_scene["part"], key="customs-no-article")
    request.lines.update(article="")
    request = take(request, sale_scene["admin"])
    sale = prepare_request_sale(
        request_id=request.pk, by=sale_scene["admin"], create_customer=True
    )
    complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=sale_scene["admin"])

    lines = [row for row in canonical_customs_lines() if row["document_id"] == sale.pk]
    assert len(lines) == 1
    assert lines[0]["number"] == ""


def test_request_article_cannot_cross_part_identity(sale_scene):
    request = make_request(sale_scene["part"], key="customs-other-part")
    request = take(request, sale_scene["admin"])
    sale = prepare_request_sale(
        request_id=request.pk, by=sale_scene["admin"], create_customer=True
    )
    complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=sale_scene["admin"])
    other = PartType.objects.create(
        name="Другая деталь", category=sale_scene["part"].category,
        unit=sale_scene["part"].unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    request.lines.update(part_type=other)

    lines = [row for row in canonical_customs_lines() if row["document_id"] == sale.pk]
    assert len(lines) == 1
    assert lines[0]["number"] == ""


def test_scanner_snapshot_takes_precedence_over_request_snapshot(sale_scene):
    request = take(
        make_request(sale_scene["part"], key="customs-scan-precedence"),
        sale_scene["admin"],
    )
    sale = prepare_request_sale(request_id=request.pk, by=sale_scene["admin"], create_customer=True)
    complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=sale_scene["admin"])
    PartNumber.objects.create(
        part=sale_scene["part"], value="SCANNED-2", kind=PartNumber.Kind.ARTICLE,
    )
    WarehouseAction.objects.create(
        action_type=WarehouseAction.Type.SALE, part_type=sale_scene["part"],
        part_number="SCANNED-2", part_name=sale_scene["part"].name,
        location=sale_scene["location"], quantity=Decimal("1"),
        customer_comment="Тест", sale=sale,
    )
    lines = [row for row in canonical_customs_lines() if row["document_id"] == sale.pk]
    assert [row["number"] for row in lines] == ["SCANNED-2"]


def test_multiple_lots_and_customs_order_keep_request_article(sale_scene):
    part = sale_scene["part"]
    from apps.actions.models import PartCustomsInfo

    customs = PartCustomsInfo.objects.get(part_type=part)
    customs.customs_unit_price_usd = Decimal("10")
    customs.save(update_fields=["customs_unit_price_usd", "updated_at"])
    batch = Batch.objects.create(supplier=sale_scene["lot"].batch.supplier)
    batch_line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("2"), unit_cost_currency=Decimal("100")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    batch = finalize_cost(batch, sale_scene["admin"])
    batch_line.refresh_from_db()
    lot2 = create_stock_lot(batch_line, sale_scene["location"], Decimal("2"))
    receive_stock_lot(lot2, by=sale_scene["admin"])
    request, _ = create_customer_request(
        customer_name="Синтетический клиент", customer_phone="+79090000004",
        preferred_messenger=CustomerRequest.Messenger.MAX,
        lines=[RequestLineInput(part_id=part.pk, quantity="4")],
        privacy_policy_version="test", personal_data_consent_version="test",
        submission_key="customs-multi-lot",
    )
    request = take(request, sale_scene["admin"])
    sale = prepare_request_sale(request_id=request.pk, by=sale_scene["admin"], create_customer=True)
    complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=sale_scene["admin"])
    assert sale.lines.count() == 2
    assert set(sale.lines.values_list("stock_lot_id", flat=True)) == {
        sale_scene["lot"].pk, lot2.pk
    }
    sources = [row for row in eligible_customs_sources(CustomsOrder.OrderType.ANALOG)
               if row["document_number"] == sale.number]
    assert len(sources) == 2
    assert {row["number"] for row in sources} == {"REQ-1"}
    assert sum((row["quantity"] for row in sources), Decimal("0")) == Decimal("4")

    # A historical order freezes the request article, not the catalog's later spelling.
    sources = eligible_customs_sources(CustomsOrder.OrderType.ANALOG)
    before_movements = StockMovement.objects.count()
    order = create_customs_order_from_boundary(
        number=1, boundary_source=(sources[-1]["source"], sources[-1]["source_id"]),
        selection_token=selection_payload(sources, order_type=CustomsOrder.OrderType.ANALOG),
        order_type=CustomsOrder.OrderType.ANALOG, by=sale_scene["admin"],
    )
    assert list(order.lines.values_list("article", flat=True)) == ["REQ-1", "REQ-1"]
    assert order.total_quantity == Decimal("4")
    assert StockMovement.objects.count() == before_movements
