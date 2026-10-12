import pytest
from django.contrib.auth import get_user_model
from django.db.models.deletion import ProtectedError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.customers.admin import CustomerAdmin
from apps.customers.models import Customer, CustomerLifecycleEvent
from apps.customers.services import (
    CustomerArchiveError,
    archive_customer,
    customers_by_recent_activity,
    restore_customer,
    search_customers,
)
from apps.repairs.models import RepairOrder
from apps.repairs.services import RepairError, create_repair_order
from apps.sales.forms import ReservationForm, SaleForm
from apps.sales.models import Sale
from apps.sales.services import ReservationError, SaleError, create_reservation, create_sale


@pytest.fixture
def operator(db):
    return get_user_model().objects.create_superuser("archive-operator", password="test")


def test_archive_retains_document_references_and_snapshots(operator):
    customer = Customer.objects.create(name="Клиент до архива", phone="+79991112233")
    sale = create_sale(customer=customer, by=operator)
    repair = create_repair_order(customer=customer, by=operator)
    reservation = create_reservation(customer=customer, by=operator)
    # These synthetic historical rows exercise archive preservation without
    # issuing stock in a customer-lifecycle test.
    Sale.objects.filter(pk=sale.pk).update(status=Sale.Status.COMPLETED, sold_at=timezone.now())
    RepairOrder.objects.filter(pk=repair.pk).update(
        status=RepairOrder.Status.COMPLETED, completed_at=timezone.now()
    )
    original = {
        "sale": (sale.customer_id, sale.customer_name, sale.customer_phone),
        "repair": (repair.customer_id, repair.customer_name, repair.customer_phone),
        "reservation": (
            reservation.customer_id,
            reservation.customer_name,
            reservation.customer_phone,
        ),
    }

    archive_customer(customer_id=customer.pk, by=operator, reason="Дубликат по просьбе клиента")

    customer.refresh_from_db()
    sale.refresh_from_db()
    repair.refresh_from_db()
    reservation.refresh_from_db()
    assert customer.is_archived
    assert sale.status == Sale.Status.COMPLETED
    assert repair.status == RepairOrder.Status.COMPLETED
    assert customer.archived_at is not None
    assert customer.archived_by_id == operator.pk
    assert (sale.customer_id, sale.customer_name, sale.customer_phone) == original["sale"]
    assert (repair.customer_id, repair.customer_name, repair.customer_phone) == original["repair"]
    assert (reservation.customer_id, reservation.customer_name, reservation.customer_phone) == (
        *original["reservation"],
    )
    event = customer.lifecycle_events.get()
    assert event.actor_id == operator.pk
    assert event.reason == "Дубликат по просьбе клиента"
    assert event.previous_status == CustomerLifecycleEvent.Status.ACTIVE
    assert event.resulting_status == CustomerLifecycleEvent.Status.ARCHIVED


def test_archived_customer_is_excluded_from_active_search_and_forms(db):
    operator = get_user_model().objects.create_superuser("archive-search", password="test")
    active = Customer.objects.create(name="Иванов", phone="+79990000001")
    archived = Customer.objects.create(name="Иванов", phone="+79990000001")
    archive_customer(customer_id=archived.pk, by=operator, reason="Неактуальная карточка")

    assert list(search_customers("Иванов")) == [active]
    assert list(search_customers("+79990000001")) == [active]
    assert list(search_customers("Иванов", include_archived=True)) == [active, archived]
    assert list(customers_by_recent_activity()) == [active]
    for form in (SaleForm(), ReservationForm()):
        assert list(form.fields["customer"].queryset) == [active]


def test_archive_and_restore_require_reason_and_preserve_audit_history(operator):
    customer = Customer.objects.create(name="Без документов")
    with pytest.raises(CustomerArchiveError, match="причину"):
        archive_customer(customer_id=customer.pk, by=operator, reason=" ")

    archive_customer(customer_id=customer.pk, by=operator, reason="Нет действующего договора")
    with pytest.raises(CustomerArchiveError, match="уже находится"):
        archive_customer(customer_id=customer.pk, by=operator, reason="Повтор")
    with pytest.raises(CustomerArchiveError, match="причину"):
        restore_customer(customer_id=customer.pk, by=operator, reason="")
    restore_customer(customer_id=customer.pk, by=operator, reason="Клиент вернулся")

    customer.refresh_from_db()
    events = list(customer.lifecycle_events.order_by("created_at"))
    assert not customer.is_archived
    assert customer.archived_by_id is None
    assert [(event.previous_status, event.resulting_status, event.reason) for event in events] == [
        ("active", "archived", "Нет действующего договора"),
        ("archived", "active", "Клиент вернулся"),
    ]
    with pytest.raises(ProtectedError):
        customer.delete()


def test_new_operations_reject_archived_customer(operator):
    customer = Customer.objects.create(name="Архивная карточка")
    archive_customer(customer_id=customer.pk, by=operator, reason="Заявка клиента")

    with pytest.raises(SaleError, match="архиве"):
        create_sale(customer=customer, by=operator)
    with pytest.raises(RepairError, match="архиве"):
        create_repair_order(customer=customer, by=operator)
    with pytest.raises(ReservationError, match="архиве"):
        create_reservation(customer=customer, by=operator)


def test_customer_archive_confirmation_permissions_csrf_and_list(operator):
    customer = Customer.objects.create(name="Клиент для UI")
    Customer.objects.create(name="Другой активный клиент")
    client = Client(enforce_csrf_checks=True)
    client.force_login(operator)
    url = reverse("customer_archive", args=[customer.pk])
    confirm = client.get(url)
    assert confirm.status_code == 200
    body = confirm.content.decode()
    assert "Архивировать клиента" in body
    assert operator.username in body
    assert "Причина" in body
    assert client.post(url, {"reason": "Без подтверждения"}).status_code == 403

    token = confirm.cookies["csrftoken"].value
    response = client.post(
        url,
        {"reason": "Карточка ошибочная", "confirm_transition": "yes"},
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 302
    customer.refresh_from_db()
    assert customer.is_archived
    assert client.get(reverse("customer_list")).status_code == 200
    assert "Клиент для UI" not in client.get(reverse("customer_list")).content.decode()
    archive_page = client.get(reverse("customer_list"), {"archived": "1"})
    assert "Клиент для UI" in archive_page.content.decode()
    assert "Другой активный клиент" not in archive_page.content.decode()
    assert 'name="archived" value="1"' in archive_page.content.decode()
    archive_search = client.get(reverse("customer_list"), {"archived": "1", "q": "Клиент"})
    assert "Клиент для UI" in archive_search.content.decode()
    assert "Другой активный клиент" not in archive_search.content.decode()
    detail = client.get(reverse("customer_detail", args=[customer.pk])).content.decode()
    assert "Восстановить клиента" in detail
    assert "Карточка в архиве" in detail
    restore_url = reverse("customer_restore", args=[customer.pk])
    restore_page = client.get(restore_url)
    restore_response = client.post(
        restore_url,
        {"reason": "Проверено, карточка нужна", "confirm_transition": "yes"},
        HTTP_X_CSRFTOKEN=restore_page.cookies["csrftoken"].value,
    )
    assert restore_response.status_code == 302
    customer.refresh_from_db()
    assert not customer.is_archived
    assert customer.lifecycle_events.count() == 2


def test_non_manager_cannot_archive_customer(operator):
    customer = Customer.objects.create(name="Защищённая карточка")
    viewer = get_user_model().objects.create_user("archive-viewer", password="test")
    client = Client()
    client.force_login(viewer)
    assert client.get(reverse("customer_archive", args=[customer.pk])).status_code == 403
    assert CustomerAdmin(Customer, None).has_delete_permission(None) is False


def test_malformed_customer_query_parameter_does_not_break_sale_cart(operator):
    client = Client()
    client.force_login(operator)
    response = client.get(reverse("actions_scan"), {"kind": "sale", "customer_id": "invalid"})
    assert response.status_code == 200


def test_archived_customer_cannot_be_edited_through_an_old_url(operator):
    customer = Customer.objects.create(name="Имя до архива")
    archive_customer(customer_id=customer.pk, by=operator, reason="Закрыта карточка")
    client = Client()
    client.force_login(operator)
    url = reverse("customer_edit", args=[customer.pk])
    assert client.get(url).status_code == 404
    assert client.post(url, {"name": "Попытка правки"}).status_code == 404
    customer.refresh_from_db()
    assert customer.name == "Имя до архива"


def test_active_customer_edit_still_saves_the_validated_fields(operator):
    customer = Customer.objects.create(name="Старое имя")
    client = Client()
    client.force_login(operator)
    response = client.post(
        reverse("customer_edit", args=[customer.pk]),
        {"name": "Новое имя", "phone": "+7 900 123-45-67"},
    )
    assert response.status_code == 302
    customer.refresh_from_db()
    assert customer.name == "Новое имя"
    assert customer.phone == "+7 900 123-45-67"
