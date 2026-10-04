"""Piece parts are counted in whole numbers; only oil is fractional (liters).

What a quantity measures comes from PartType.is_oil (apps.catalog.quantity_units).
Every entry point that creates a request, sale, reservation, repair or write-off
line for a piece part refuses 0.5 / 1.5 / 2.001 explicitly - never rounding,
flooring or ceiling them - while oil keeps its 0.001 L precision. Historical
fractional rows are not touched: they stay visible as they are and are not
silently turned into whole numbers on their way to a new document.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.actions.cart import add_scan, open_cart
from apps.actions.services import ActionError, perform_action
from apps.catalog.public_cart import CartError, parse_quantity
from apps.catalog.quantity_units import (
    PIECE_QUANTITY_ERROR,
    format_quantity,
    validate_part_quantity,
)
from apps.customer_accounts.models import Provider
from apps.customer_requests.customer_cabinet import (
    build_reorder_preview,
    create_request_from_reorder_preview,
)
from apps.customer_requests.customer_ui import reorder_preview_text
from apps.customer_requests.models import CustomerRequest, CustomerRequestLine
from apps.customer_requests.sale_conversion import (
    CustomerRequestSaleError,
    complete_request_sale,
    prepare_request_sale,
)
from apps.customer_requests.services import (
    CustomerRequestError,
    RequestLineInput,
    change_request_status,
    create_customer_request,
)
from apps.inventory.models import StockLot
from apps.repairs.models import RepairIssueLine
from apps.repairs.services import (
    RepairError,
    add_oil_volume_to_repair_order,
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.sales.models import ReservationLine, Sale, SaleLine
from apps.sales.services import (
    ReservationError,
    SaleError,
    activate_reservation,
    add_oil_volume_to_sale,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    cancel_sale_line_quantity,
    complete_sale,
    create_reservation,
    create_sale,
    create_sale_from_reservation,
)
from apps.writeoffs.models import WriteOffDocument
from apps.writeoffs.services import (
    WriteOffError,
    add_stock_lot_to_write_off,
    create_write_off,
    quick_write_off,
)
from tests.customer_account_support import make_customer, make_sale
from tests.customs_support import remember_customs
from tests.test_customer_requests import POLICY
from tests.test_messenger_customer_cabinet import _identity

pytestmark = pytest.mark.django_db

FRACTIONS = ["0.5", "1.5", "2.001"]


@pytest.fixture(autouse=True)
def cabinet_flags(settings):
    settings.CUSTOMER_MESSENGER_CABINET_ENABLED = True
    settings.CUSTOMER_MESSENGER_REPEAT_CONSENT_VERSION = "messenger-repeat-v1"


@pytest.fixture
def scene(public_catalog):
    piece = public_catalog.part("Фильтр масляный", article="PC-1", price="1500")
    oil = public_catalog.part("Масло 4T", article="OIL-1", price="4000")
    oil.is_oil = True
    oil.oil_package_volume_l = Decimal("4")
    oil.save(update_fields=["is_oil", "oil_package_volume_l"])
    remember_customs(piece, oil)
    return {
        "admin": public_catalog.user,
        "location": public_catalog.location,
        "piece": piece,
        "piece_lot": public_catalog.stock(piece, "10"),
        "oil": oil,
        "oil_lot": public_catalog.stock(oil, "20"),
    }


def _lines(model, **filters):
    return model.objects.filter(**filters).count()


# --- The shared rule ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "2", "15", "1.000"])
def test_whole_piece_quantities_are_valid(scene, value):
    assert validate_part_quantity(Decimal(value), scene["piece"]) is None


@pytest.mark.parametrize("value", FRACTIONS)
def test_fractional_piece_quantities_are_invalid(scene, value):
    assert validate_part_quantity(Decimal(value), scene["piece"]) == PIECE_QUANTITY_ERROR


@pytest.mark.parametrize("value", ["0.5", "1.5", "2.75", "0.001", "3"])
def test_oil_keeps_fractional_liters(scene, value):
    assert validate_part_quantity(Decimal(value), scene["oil"]) is None


# --- A. Request service ------------------------------------------------------------------


def _create_request(part, quantity, key):
    return create_customer_request(
        customer_name="Ольга Смирнова",
        customer_phone="+7 (912) 555-44-33",
        preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
        lines=[RequestLineInput(part_id=part.pk, quantity=quantity, supply_inquiry=False)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=key,
    )


@pytest.mark.parametrize("value", ["1", "2"])
def test_request_service_accepts_whole_piece_quantities(scene, value):
    request, created = _create_request(scene["piece"], value, f"piece-ok-{value}".ljust(40, "k"))
    assert created
    assert request.lines.get().quantity_requested == Decimal(value)


@pytest.mark.parametrize("value", ["1.5", "0.5"])
def test_request_service_refuses_fractional_piece_quantities(scene, value):
    with pytest.raises(CustomerRequestError, match="целым"):
        _create_request(scene["piece"], value, f"piece-bad-{value}".ljust(40, "k"))
    assert not CustomerRequest.objects.exists()


@pytest.mark.parametrize("value", ["0.5", "1.5"])
def test_request_service_keeps_oil_fractions_as_before(scene, value):
    request, created = _create_request(scene["oil"], value, f"oil-ok-{value}".ljust(40, "k"))
    assert created and request.lines.get().quantity_requested == Decimal(value)


# --- B. Public catalog -------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1.5", "0,5", "2.001"])
def test_public_cart_accepts_only_whole_quantities(raw):
    with pytest.raises(CartError):
        parse_quantity(raw)
    assert parse_quantity("2") == 2


# --- C. Manual sale, reservation, repair, write-off (services and forms) -----------------


@pytest.mark.parametrize("value", FRACTIONS)
def test_manual_sale_service_refuses_fractional_pieces(scene, value):
    sale = create_sale(customer_name="Клиент", by=scene["admin"])
    with pytest.raises(SaleError, match="целым"):
        add_stock_lot_to_sale(sale, scene["piece_lot"], Decimal(value), unit_price=Decimal("1"))
    assert not SaleLine.objects.exists()
    add_stock_lot_to_sale(sale, scene["piece_lot"], Decimal("2"), unit_price=Decimal("1"))
    assert SaleLine.objects.get().quantity == Decimal("2")


def test_manual_sale_oil_volume_stays_fractional(scene):
    sale = create_sale(customer_name="Клиент", by=scene["admin"])
    line = add_oil_volume_to_sale(sale, scene["oil_lot"], Decimal("1.5"), by=scene["admin"])
    assert line.quantity == Decimal("1.5")


def test_manual_sale_form_refuses_a_fractional_piece_without_a_server_error(scene, client):
    client.force_login(scene["admin"])
    sale = create_sale(customer_name="Клиент", by=scene["admin"])
    response = client.post(
        reverse("sale_add_lot", args=[sale.pk]),
        {"lot": scene["piece_lot"].pk, "quantity": "1.5", "unit_price": "100"},
        follow=True,
    )
    assert response.status_code == 200
    assert "целым" in response.content.decode()
    assert not SaleLine.objects.exists()


@pytest.mark.parametrize("value", FRACTIONS)
def test_reservation_service_refuses_fractional_pieces(scene, value):
    reservation = create_reservation(customer_name="Клиент", by=scene["admin"])
    with pytest.raises(ReservationError, match="целым"):
        add_stock_lot_to_reservation(reservation, scene["piece_lot"], Decimal(value))
    assert not reservation.lines.exists()


def test_reservation_form_refuses_a_fractional_piece_without_a_server_error(scene, client):
    client.force_login(scene["admin"])
    reservation = create_reservation(customer_name="Клиент", by=scene["admin"])
    response = client.post(
        reverse("reservation_add_lot", args=[reservation.pk]),
        {"lot": scene["piece_lot"].pk, "quantity": "1.5"},
        follow=True,
    )
    assert response.status_code == 200
    assert "целым" in response.content.decode()
    assert not reservation.lines.exists()


@pytest.mark.parametrize("value", FRACTIONS)
def test_repair_service_refuses_fractional_pieces_and_keeps_oil(scene, value):
    order = create_repair_order(customer_name="Клиент", by=scene["admin"])
    with pytest.raises(RepairError, match="целым"):
        add_stock_lot_to_repair_order(order, scene["piece_lot"], Decimal(value))
    oil_line = add_oil_volume_to_repair_order(order, scene["oil_lot"], Decimal("1.5"))
    assert oil_line.quantity == Decimal("1.5")
    assert order.lines.count() == 1


def test_repair_form_refuses_a_fractional_piece_without_a_server_error(scene, client):
    client.force_login(scene["admin"])
    order = create_repair_order(customer_name="Клиент", by=scene["admin"])
    response = client.post(
        reverse("repair_order_add_lot", args=[order.pk]),
        {"lot": scene["piece_lot"].pk, "quantity": "1.5"},
        follow=True,
    )
    assert response.status_code == 200
    assert "целым" in response.content.decode()
    assert not order.lines.exists()


@pytest.mark.parametrize("value", FRACTIONS)
def test_write_off_services_refuse_fractional_pieces(scene, value):
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=scene["admin"])
    with pytest.raises(WriteOffError, match="целым"):
        add_stock_lot_to_write_off(doc, scene["piece_lot"], Decimal(value))
    with pytest.raises(WriteOffError, match="целым"):
        quick_write_off(
            part=scene["piece"], scanned_code="PC-1", reason="Брак", business_author="Денис",
            quantity=value, location_id=scene["location"].pk, by=scene["admin"],
        )
    assert StockLot.objects.get(pk=scene["piece_lot"].pk).quantity == Decimal("10")


@pytest.mark.parametrize("value", ["1,5", "0.5"])
def test_quick_actions_refuse_fractional_pieces(scene, value):
    with pytest.raises(ActionError, match="целым"):
        perform_action(
            part=scene["piece"], location=scene["location"], action_type="sale",
            quantity=value, customer_comment="Клиент", by=scene["admin"],
        )
    cart = open_cart("sale", by=scene["admin"])
    with pytest.raises(ActionError, match="целым"):
        add_scan(cart, scene["piece"], scene["location"], quantity=value, by=scene["admin"])
    assert not SaleLine.objects.exists()
    assert StockLot.objects.get(pk=scene["piece_lot"].pk).quantity == Decimal("10")


def test_write_off_form_refuses_a_fractional_piece_without_a_server_error(scene, client):
    client.force_login(scene["admin"])
    doc = create_write_off(reason=WriteOffDocument.Reason.OTHER, by=scene["admin"])
    response = client.post(
        reverse("write_off_add_lot", args=[doc.pk]),
        {"lot": scene["piece_lot"].pk, "quantity": "0.5"},
        follow=True,
    )
    assert response.status_code == 200
    assert "целым" in response.content.decode()
    assert not doc.lines.exists()


# --- F. The invariant holds when forms and add-line services are bypassed ---------------


def test_a_legacy_fractional_reservation_never_completes_as_a_sale(scene):
    reservation = create_reservation(customer_name="Клиент", by=scene["admin"])
    add_stock_lot_to_reservation(reservation, scene["piece_lot"], Decimal("2"))
    reservation = activate_reservation(reservation, by=scene["admin"])
    ReservationLine.objects.filter(reservation=reservation).update(quantity=Decimal("1.5"))

    sale = create_sale_from_reservation(reservation, by=scene["admin"])
    with pytest.raises(SaleError, match="целым"):
        complete_sale(sale, by=scene["admin"])

    sale.refresh_from_db()
    assert sale.status == Sale.Status.DRAFT
    assert StockLot.objects.get(pk=scene["piece_lot"].pk).quantity == Decimal("10")


def test_a_legacy_fractional_repair_draft_is_never_issued(scene):
    order = create_repair_order(customer_name="Клиент", by=scene["admin"])
    add_stock_lot_to_repair_order(order, scene["piece_lot"], Decimal("2"))
    RepairIssueLine.objects.filter(repair_order=order).update(quantity=Decimal("1.5"))

    with pytest.raises(RepairError, match="целым"):
        complete_repair_order(order, by=scene["admin"])

    assert StockLot.objects.get(pk=scene["piece_lot"].pk).quantity == Decimal("10")


def test_oil_liters_still_complete_in_sales_and_repairs(scene):
    sale = create_sale(customer_name="Клиент", by=scene["admin"])
    add_oil_volume_to_sale(sale, scene["oil_lot"], Decimal("1.5"), by=scene["admin"])
    sale = complete_sale(sale, by=scene["admin"])
    order = create_repair_order(customer_name="Клиент", by=scene["admin"])
    add_oil_volume_to_repair_order(order, scene["oil_lot"], Decimal("0.75"))
    order = complete_repair_order(order, by=scene["admin"])

    assert sale.status == Sale.Status.COMPLETED
    assert StockLot.objects.get(pk=scene["oil_lot"].pk).quantity == Decimal("17.75")


def _completed_piece_sale(scene, quantity="2"):
    sale = create_sale(customer_name="Клиент", by=scene["admin"])
    add_stock_lot_to_sale(sale, scene["piece_lot"], Decimal(quantity), unit_price=Decimal("100"))
    return complete_sale(sale, by=scene["admin"])


def test_part_of_a_piece_line_is_never_cancelled_as_a_fraction(scene):
    sale = _completed_piece_sale(scene)
    line = sale.lines.get()
    with pytest.raises(SaleError, match="целым"):
        cancel_sale_line_quantity(line, "0.5", reason="Ошибка", author="Денис")
    assert StockLot.objects.get(pk=scene["piece_lot"].pk).quantity == Decimal("8")
    cancel_sale_line_quantity(line, "1", reason="Ошибка", author="Денис")
    assert StockLot.objects.get(pk=scene["piece_lot"].pk).quantity == Decimal("9")


def test_a_legacy_fractional_piece_line_can_still_be_cancelled_in_full(scene):
    sale = _completed_piece_sale(scene)
    line = sale.lines.get()
    SaleLine.objects.filter(pk=line.pk).update(quantity=Decimal("1.5"))  # legacy row
    with pytest.raises(SaleError, match="целым"):
        cancel_sale_line_quantity(line, "0.5", reason="Ошибка", author="Денис")
    document = cancel_sale_line_quantity(line, "1.5", reason="Ошибка", author="Денис")
    assert document.lines.get().quantity == Decimal("1.5")


# --- D. Customer request -> Sale ---------------------------------------------------------


def test_a_legacy_fractional_piece_request_cannot_become_a_sale(scene):
    request, _ = _create_request(scene["piece"], "2", "legacy-fraction".ljust(40, "k"))
    # A row stored before this validation existed (direct write, as legacy data is).
    CustomerRequestLine.objects.filter(request=request).update(quantity_requested=Decimal("1.5"))
    change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.IN_PROGRESS,
        by=scene["admin"],
    )

    with pytest.raises(CustomerRequestSaleError, match="целым"):
        prepare_request_sale(request_id=request.pk, by=scene["admin"], create_customer=True)

    assert not Sale.objects.exists() and not SaleLine.objects.exists()
    assert CustomerRequestLine.objects.get(request=request).quantity_requested == Decimal("1.5")


def test_a_draft_holding_a_fractional_piece_is_never_completed(scene):
    request, _ = _create_request(scene["piece"], "2", "legacy-draft-fraction".ljust(40, "k"))
    change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.IN_PROGRESS,
        by=scene["admin"],
    )
    sale = prepare_request_sale(request_id=request.pk, by=scene["admin"], create_customer=True)
    # Request and draft both carried 1.5 before this validation existed.
    CustomerRequestLine.objects.filter(request=request).update(quantity_requested=Decimal("1.5"))
    SaleLine.objects.filter(sale=sale).update(quantity=Decimal("1.5"))

    with pytest.raises(CustomerRequestSaleError, match="целым"):
        complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=scene["admin"])

    sale.refresh_from_db()
    assert sale.status == Sale.Status.DRAFT
    assert StockLot.objects.get(pk=scene["piece_lot"].pk).quantity == Decimal("10")


# --- E. Cabinet repeat purchase ----------------------------------------------------------


def test_repeat_purchase_of_whole_pieces_stays_whole(scene):
    customer = make_customer("Алиса")
    customer.phone = "+79125554433"
    customer.save(update_fields=["phone"])
    sale = make_sale(customer, scene["piece"], lot=scene["piece_lot"], quantity="2")
    _identity(customer, 7001, provider=Provider.TELEGRAM, admin=scene["admin"])

    request, created = create_request_from_reorder_preview(
        provider=Provider.TELEGRAM, provider_user_id=7001, sale_id=sale.pk,
        submission_key="reorder-whole-pieces-7001-0000000001",
    )

    assert created and request.lines.get().quantity_requested == Decimal("2")


def test_a_historical_fractional_piece_purchase_is_never_repeated_as_a_whole_number(scene):
    customer = make_customer("Борис")
    customer.phone = "+79125554434"
    customer.save(update_fields=["phone"])
    sale = make_sale(customer, scene["piece"], lot=scene["piece_lot"], quantity="1.5")
    _identity(customer, 7002, provider=Provider.TELEGRAM, admin=scene["admin"])

    preview = build_reorder_preview(
        provider=Provider.TELEGRAM, provider_user_id=7002, sale_id=sale.pk
    )
    line = preview.lines[0]
    assert line.available is False
    assert line.requested_quantity == Decimal("0")
    assert "уточните" in line.reason
    with pytest.raises(Exception, match="недоступна|нет доступных"):
        create_request_from_reorder_preview(
            provider=Provider.TELEGRAM, provider_user_id=7002, sale_id=sale.pk,
            submission_key="reorder-fraction-pieces-7002-00000001",
        )
    assert not CustomerRequestLine.objects.filter(
        quantity_requested__in=[Decimal("1"), Decimal("2")]
    ).exists()


def test_the_messenger_preview_names_the_fractional_line_and_its_reason(scene):
    customer = make_customer("Вера")
    customer.phone = "+79125554435"
    customer.save(update_fields=["phone"])
    sale = make_sale(customer, scene["piece"], lot=scene["piece_lot"], quantity="1.5")
    _identity(customer, 7003, provider=Provider.TELEGRAM, admin=scene["admin"])

    text = reorder_preview_text(
        build_reorder_preview(provider=Provider.TELEGRAM, provider_user_id=7003, sale_id=sale.pk)
    )

    assert "Фильтр масляный" in text and "уточните у менеджера" in text


# --- Display does not regress -----------------------------------------------------------


def test_quantities_read_as_before(scene):
    assert format_quantity(Decimal("1.000"), scene["piece"]) == "1"
    assert format_quantity(Decimal("15.000"), scene["piece"]) == "15"
    assert format_quantity(Decimal("2.500"), scene["oil"]) == "2,5"
    assert format_quantity(Decimal("2.750"), scene["oil"]) == "2,75"
    # A legacy fractional piece stays visible as it is, never rounded.
    assert format_quantity(Decimal("1.500"), scene["piece"]) == "1,5"


# --- Legacy audit (read only) -----------------------------------------------------------


def test_the_legacy_audit_lists_fractional_piece_rows_and_changes_nothing(scene):
    from io import StringIO

    from django.core.management import call_command

    customer = make_customer("Глеб")
    make_sale(customer, scene["piece"], lot=scene["piece_lot"], quantity="1.5")
    make_sale(customer, scene["piece"], lot=scene["piece_lot"], quantity="2")
    make_sale(customer, scene["oil"], lot=scene["oil_lot"], quantity="2.5")
    request, _ = _create_request(scene["piece"], "2", "audit-request".ljust(40, "k"))
    CustomerRequestLine.objects.filter(request=request).update(quantity_requested=Decimal("0.5"))
    before = sorted(SaleLine.objects.values_list("pk", "quantity"))

    out = StringIO()
    call_command("audit_piece_quantities", stdout=out)
    text = out.getvalue()

    assert "Дробных штучных строк всего: 2" in text
    assert "Строки продаж: 1 (completed: 1)" in text
    assert "Строки заявок клиентов: 1" in text
    assert "; 1,5" in text and "; 0,5" in text
    assert "2,5" not in text  # oil liters are not an anomaly
    assert "Глеб" not in text and "Ольга" not in text  # no customer content
    assert sorted(SaleLine.objects.values_list("pk", "quantity")) == before
    assert CustomerRequestLine.objects.get(request=request).quantity_requested == Decimal("0.5")


def test_the_legacy_audit_reports_a_clean_database(scene):
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("audit_piece_quantities", stdout=out)
    assert "Итог: дробных штучных строк нет." in out.getvalue()
