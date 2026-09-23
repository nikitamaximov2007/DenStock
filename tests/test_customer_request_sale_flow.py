"""RC contract for safe CustomerRequest -> Customer -> Sale conversion."""
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.core.phones import normalize_phone
from apps.customer_requests.models import CustomerRequest
from apps.customer_requests.sale_conversion import (
    CustomerRequestSaleError,
    complete_request_sale,
    match_request_customer,
    prepare_request_sale,
)
from apps.customer_requests.services import (
    RequestLineInput,
    change_request_status,
    create_customer_request,
)
from apps.customers.models import Customer
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Sale, SaleLine
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from tests.customs_support import remember_customs

PASSWORD = "parol-12345"
POLICY = "request-sale-test"


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="request-admin", password=PASSWORD)


@pytest.fixture
def sale_scene(admin):
    category = Category.objects.create(name="Запчасти заявки")
    unit = Unit.objects.get(name="Штука")
    manufacturer = Manufacturer.objects.create(name="BRP request")
    part = PartType.objects.create(
        name="Деталь для заявки",
        category=category,
        unit=unit,
        manufacturer=manufacturer,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("1000"),
        is_public=True,
    )
    PartNumber.objects.create(part=part, value="REQ-1", is_primary=True)
    supplier = Supplier.objects.create(name="Поставщик заявок")
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    batch_line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("5"), unit_cost_currency=Decimal("100")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    batch = finalize_cost(batch, admin)
    batch_line.refresh_from_db()
    location = StorageLocation.objects.create(
        name="Заявки A", code="REQ-A", storage_allowed=True, is_active=True
    )
    lot = create_stock_lot(batch_line, location, Decimal("3"))
    receive_stock_lot(lot, by=admin)
    remember_customs(part)
    return {"admin": admin, "part": part, "lot": lot, "location": location}


def make_request(part, *, phone="89090000001", name="Александр Пушкарев", key="request-key"):
    key = f"customer-request-{key}"
    request, _created = create_customer_request(
        customer_name=name,
        customer_phone=phone,
        preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
        lines=[RequestLineInput(part_id=part.pk, quantity="1", supply_inquiry=False)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=key,
    )
    return request


def take(request, admin):
    change_request_status(
        request_id=request.pk, target_status=CustomerRequest.Status.IN_PROGRESS, by=admin
    )
    request.refresh_from_db()
    return request


@pytest.mark.parametrize(
    "phone",
    [
        "89090000001",
        "79090000001",
        "+79090000001",
        "+7 (909) 000-00-01",
        "+7 909 000 00 01",
        "8 (909) 000-00-01",
    ],
)
def test_phone_formats_match_one_customer(sale_scene, phone):
    Customer.objects.create(name="Канонический клиент", phone="+79090000001")
    request = make_request(sale_scene["part"], phone=phone, key=f"phone-{normalize_phone(phone)}")

    match = match_request_customer(request)

    assert normalize_phone(phone) == "79090000001"
    assert match.count == 1
    assert match.customers[0].name == "Канонический клиент"


def test_different_phone_does_not_match(sale_scene):
    Customer.objects.create(name="Другой номер", phone="+79090000001")
    request = make_request(sale_scene["part"], phone="+79090000002", key="different-phone")

    assert match_request_customer(request).count == 0


def test_legacy_customer_phone_is_normalized_at_match_time(sale_scene):
    customer = Customer.objects.create(name="Старая карточка", phone="8 (909) 000-00-01")
    Customer.objects.filter(pk=customer.pk).update(phone_normalized="")
    request = make_request(sale_scene["part"], key="legacy-phone-match")

    assert match_request_customer(request).customers[0].pk == customer.pk


def test_take_in_work_has_no_sale_or_stock_effect(sale_scene):
    request = make_request(sale_scene["part"], key="take-only")
    before_movements = StockMovement.objects.count()
    before_sales = Sale.objects.count()
    before_quantity = sale_scene["lot"].quantity

    request = take(request, sale_scene["admin"])

    assert request.status == CustomerRequest.Status.IN_PROGRESS
    assert request.taken_by_id == sale_scene["admin"].pk
    assert request.customer_id is None
    assert request.sale_id is None
    assert Sale.objects.count() == before_sales
    assert StockMovement.objects.count() == before_movements
    sale_scene["lot"].refresh_from_db()
    assert sale_scene["lot"].quantity == before_quantity


def test_existing_customer_is_linked_without_renaming_and_draft_is_prefilled(sale_scene):
    customer = Customer.objects.create(name="Александр Пушкарёв", phone="+7 909 000-00-01")
    request = take(make_request(sale_scene["part"], key="existing-customer"), sale_scene["admin"])

    sale = prepare_request_sale(request_id=request.pk, by=sale_scene["admin"])
    request.refresh_from_db()
    customer.refresh_from_db()
    sale.refresh_from_db()

    assert request.customer_id == customer.pk
    assert request.sale_id == sale.pk
    assert customer.name == "Александр Пушкарёв"
    assert sale.status == Sale.Status.DRAFT
    assert sale.customer_id == customer.pk
    assert list(sale.lines.values_list("quantity", "unit_price")) == [
        (Decimal("1"), Decimal("1000"))
    ]
    assert StockMovement.objects.filter(document_type="sale").count() == 0


def test_no_match_requires_explicit_customer_creation(sale_scene):
    request = take(
        make_request(sale_scene["part"], phone="+79090000003", key="no-customer"),
        sale_scene["admin"],
    )

    with pytest.raises(CustomerRequestSaleError, match="Создать клиента"):
        prepare_request_sale(request_id=request.pk, by=sale_scene["admin"])
    sale = prepare_request_sale(
        request_id=request.pk, by=sale_scene["admin"], create_customer=True
    )
    assert sale.customer_id is not None
    assert Customer.objects.filter(phone_normalized="79090000003").count() == 1


def test_multiple_matches_fail_closed_until_operator_selects(sale_scene):
    first = Customer.objects.create(name="Первый", phone="+79090000001")
    second = Customer.objects.create(name="Второй", phone="8 (909) 000-00-01")
    request = take(make_request(sale_scene["part"], key="multiple-customer"), sale_scene["admin"])

    with pytest.raises(CustomerRequestSaleError, match="несколько клиентов"):
        prepare_request_sale(request_id=request.pk, by=sale_scene["admin"])
    sale = prepare_request_sale(
        request_id=request.pk, by=sale_scene["admin"], customer_id=second.pk
    )
    assert sale.customer_id == second.pk
    assert first.pk != second.pk


def test_final_sale_rechecks_current_price_and_is_idempotent(sale_scene):
    customer = Customer.objects.create(name="Покупатель", phone="+79090000001")
    request = take(make_request(sale_scene["part"], key="final-sale"), sale_scene["admin"])
    sale = prepare_request_sale(request_id=request.pk, by=sale_scene["admin"])
    sale_scene["part"].recommended_price = Decimal("1200")
    sale_scene["part"].save(update_fields=["recommended_price"])
    before_movements = StockMovement.objects.filter(document_type="sale").count()

    completed = complete_request_sale(
        request_id=request.pk, sale_id=sale.pk, by=sale_scene["admin"]
    )
    repeated = complete_request_sale(
        request_id=request.pk, sale_id=sale.pk, by=sale_scene["admin"]
    )

    assert completed.pk == repeated.pk == sale.pk
    assert Sale.objects.filter(pk=sale.pk, status=Sale.Status.COMPLETED).count() == 1
    assert SaleLine.objects.get(sale=sale).unit_price == Decimal("1200")
    assert StockMovement.objects.filter(document_type="sale").count() == before_movements + 1
    request.refresh_from_db()
    assert request.status == CustomerRequest.Status.COMPLETED
    assert request.customer_id == customer.pk
    assert CustomerRequest.objects.filter(sale_id=sale.pk).count() == 1


def test_failed_final_sale_rolls_back_stock_and_request(sale_scene):
    request = take(make_request(sale_scene["part"], key="rollback"), sale_scene["admin"])
    sale = prepare_request_sale(
        request_id=request.pk, by=sale_scene["admin"], create_customer=True
    )
    sale_scene["lot"].quantity = Decimal("0")
    sale_scene["lot"].status = sale_scene["lot"].Status.DEPLETED
    sale_scene["lot"].save(update_fields=["quantity", "status", "updated_at"])
    before_movements = StockMovement.objects.filter(document_type="sale").count()

    with pytest.raises(CustomerRequestSaleError):
        complete_request_sale(request_id=request.pk, sale_id=sale.pk, by=sale_scene["admin"])

    request.refresh_from_db()
    sale.refresh_from_db()
    assert request.status == CustomerRequest.Status.IN_PROGRESS
    assert sale.status == Sale.Status.DRAFT
    assert StockMovement.objects.filter(document_type="sale").count() == before_movements


def test_request_detail_exposes_sale_action_and_audit_is_read_only(client, sale_scene):
    request = take(make_request(sale_scene["part"], key="screen-and-audit"), sale_scene["admin"])
    client.force_login(sale_scene["admin"])

    response = client.get(reverse("customer_request_detail", args=[request.pk]))
    assert response.status_code == 200
    assert "Оформить продажу по заявке" in response.content.decode()
    before_customers = Customer.objects.count()
    output = StringIO()
    call_command("audit_customer_request_customer_matches", stdout=output)
    assert "Заявки без совпадения" in output.getvalue()
    assert Customer.objects.count() == before_customers
