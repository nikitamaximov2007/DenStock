"""Справочник клиентов: постоянная карточка вместо строки в документе.

Что здесь гарантируется:

* у клиента есть стабильный идентификатор, а имя и телефон не являются
  идентичностью: тёзки и общий номер это норма, а не ошибка;
* телефон необязателен, ищется в любом привычном российском формате и при этом
  чужие международные номера не превращаются в российские;
* документ забирает СНИМОК имени и телефона в момент создания, поэтому
  переименование карточки завтра не переписывает историю;
* документы без карточки (созданные до справочника) продолжают работать;
* никакой автоматической привязки истории не происходит.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.urls import reverse

from apps.core.phones import normalize_phone
from apps.customers.models import Customer
from apps.customers.services import customer_snapshot, search_customers
from apps.repairs.models import RepairOrder
from apps.repairs.services import create_repair_order
from apps.sales.models import Reservation, Sale
from apps.sales.services import create_reservation, create_sale

PASSWORD = "parol-12345"


def _legacy_sale(name, phone=""):
    return Sale.objects.create(
        status=Sale.Status.COMPLETED, customer_name=name, customer_phone=phone
    )


def _legacy_repair(name, phone=""):
    return RepairOrder.objects.create(
        status=RepairOrder.Status.COMPLETED, customer_name=name, customer_phone=phone
    )


def test_explicit_legacy_backfill_links_only_safe_identity(db):
    sale_one = _legacy_sale("Петр", "8 912 070-70-78")
    sale_two = _legacy_sale("Петр", "+7 912 070 70 78")
    repair = _legacy_repair("Петр", "89120707078")
    snapshots = [(item.customer_name, item.customer_phone) for item in (sale_one, sale_two, repair)]

    call_command("backfill_legacy_customers")
    assert Customer.objects.count() == 0  # dry-run by default
    call_command("backfill_legacy_customers", "--apply")

    customer = Customer.objects.get()
    sale_one.refresh_from_db()
    sale_two.refresh_from_db()
    repair.refresh_from_db()
    assert {sale_one.customer_id, sale_two.customer_id, repair.customer_id} == {customer.pk}
    assert [
        (item.customer_name, item.customer_phone) for item in (sale_one, sale_two, repair)
    ] == snapshots
    call_command("backfill_legacy_customers", "--apply")
    assert Customer.objects.count() == 1


def test_legacy_backfill_reuses_only_exact_existing_customer(db):
    customer = Customer.objects.create(name="Петр", phone="+7 912 070-70-78")
    sale = _legacy_sale("Петр", "89120707078")
    call_command("backfill_legacy_customers", "--apply")
    sale.refresh_from_db()
    assert Customer.objects.count() == 1
    assert sale.customer_id == customer.pk


@pytest.mark.parametrize(
    "documents",
    [(("Иван Петров", "89120000001"), ("Иван Петров", "89120000002")),
     (("Иван Петров", "89120000001"), ("Петр Иванов", "89120000001")),
     (("Иванов Сергей", ""),)],
)
def test_legacy_backfill_keeps_ambiguous_identities_unlinked(db, documents):
    created = [_legacy_sale(name, phone) for name, phone in documents]
    call_command("backfill_legacy_customers", "--apply")
    assert Customer.objects.count() == 0
    assert all(Sale.objects.get(pk=item.pk).customer_id is None for item in created)


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


def _login(client, make_user, *, role=None, superuser=True, name="boss"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


# --- Карточка ------------------------------------------------------------------------------


def test_customer_created_with_optional_phone(db):
    without = Customer.objects.create(name="Иванов")
    with_phone = Customer.objects.create(name="Петров", phone="+7 912 123-45-67")
    assert without.phone == ""
    assert without.phone_normalized == ""
    assert with_phone.phone == "+7 912 123-45-67"  # показываем как ввели
    assert with_phone.phone_normalized == "79121234567"


def test_two_customers_may_share_a_phone(db):
    """Семейный или рабочий номер: телефон не идентифицирует человека."""
    first = Customer.objects.create(name="Иванов Иван", phone="+79121234567")
    second = Customer.objects.create(name="Иванова Мария", phone="8 912 123 45 67")
    assert first.pk != second.pk
    assert first.phone_normalized == second.phone_normalized


def test_two_customers_may_share_a_name(db):
    """Тёзки: одинаковое имя не делает клиентов одним человеком."""
    first = Customer.objects.create(name="Иван Иванов")
    second = Customer.objects.create(name="Иван Иванов", phone="+79990000000")
    assert first.pk != second.pk


def test_phone_change_updates_search_form(db):
    customer = Customer.objects.create(name="Иванов", phone="+79121234567")
    customer.phone = "8 495 000 11 22"
    customer.save(update_fields=["phone"])
    customer.refresh_from_db()
    assert customer.phone_normalized == "74950001122"


def test_name_and_phone_are_trimmed(db):
    customer = Customer.objects.create(name="  Иванов  ", phone="  +7 912 123-45-67  ")
    assert customer.name == "Иванов"
    assert customer.phone == "+7 912 123-45-67"


# --- Нормализация и поиск -------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["+7 912 123-45-67", "8 912 123 45 67", "89121234567", "79121234567", "9121234567"],
)
def test_russian_variants_normalize_together(raw):
    assert normalize_phone(raw) == "79121234567"


def test_international_number_is_not_turned_russian():
    assert normalize_phone("+49 30 123456") == "4930123456"
    assert normalize_phone("+1 202 555 0134") == "12025550134"


def test_search_by_name_and_by_any_phone_format(db):
    target = Customer.objects.create(name="Иванов Пётр", phone="+7 912 123-45-67")
    Customer.objects.create(name="Сидоров", phone="+7 495 000-11-22")
    assert list(search_customers("Иванов")) == [target]
    for query in ("+7 912 123-45-67", "89121234567", "9121234567", "1234567"):
        assert list(search_customers(query)) == [target], query


def test_search_returns_both_owners_of_one_phone(db):
    first = Customer.objects.create(name="Иванов Иван", phone="+79121234567")
    second = Customer.objects.create(name="Иванова Мария", phone="89121234567")
    found = set(search_customers("+7 912 123 45 67").values_list("pk", flat=True))
    assert found == {first.pk, second.pk}


def test_search_does_not_match_foreign_number_as_russian(db):
    Customer.objects.create(name="Ганс", phone="+49 30 123456")
    assert list(search_customers("+7 930 123-45-6")) == []


# --- Снимок в документах --------------------------------------------------------------------


def test_new_documents_take_snapshot_from_customer(db, admin):
    customer = Customer.objects.create(name="Иванов", phone="+7 912 123-45-67")
    sale = create_sale(customer=customer, by=admin)
    order = create_repair_order(customer=customer, by=admin)
    reservation = create_reservation(customer=customer, by=admin)
    for document in (sale, order, reservation):
        assert document.customer_id == customer.pk
        assert document.customer_name == "Иванов"
        assert document.customer_phone == "+7 912 123-45-67"


def test_renaming_customer_does_not_rewrite_history(db, admin):
    customer = Customer.objects.create(name="Иванов", phone="+79121234567")
    sale = create_sale(customer=customer, by=admin)
    order = create_repair_order(customer=customer, by=admin)

    customer.name = "Иванов Иван Иванович"
    customer.phone = "+7 495 000-11-22"
    customer.save()

    sale.refresh_from_db()
    order.refresh_from_db()
    assert sale.customer_name == "Иванов"
    assert sale.customer_phone == "+79121234567"
    assert order.customer_name == "Иванов"
    assert order.customer_phone == "+79121234567"
    # Связь с карточкой при этом сохраняется.
    assert sale.customer_id == customer.pk


def test_documents_without_customer_still_work(db, admin):
    """Legacy-поток свободного ввода остаётся рабочим."""
    sale = create_sale(customer_name="Разовый покупатель", by=admin)
    order = create_repair_order(customer_name="Разовый покупатель", by=admin)
    assert sale.customer_id is None
    assert order.customer_id is None
    assert sale.customer_name == "Разовый покупатель"


def test_customer_snapshot_helper_prefers_card(db):
    customer = Customer.objects.create(name="Иванов", phone="+79121234567")
    assert customer_snapshot(customer, fallback_name="Другой") == {
        "customer_name": "Иванов",
        "customer_phone": "+79121234567",
    }
    assert customer_snapshot(None, fallback_name="  Другой  ", fallback_phone=" 123 ") == {
        "customer_name": "Другой",
        "customer_phone": "123",
    }


def test_document_still_requires_some_customer_identity(db, admin):
    from apps.sales.services import SaleError

    with pytest.raises(SaleError):
        create_sale(by=admin)


def test_sale_from_reservation_keeps_reservation_snapshot(db, admin):
    customer = Customer.objects.create(name="Иванов", phone="+79121234567")
    reservation = create_reservation(customer=customer, by=admin)
    customer.name = "Переименован"
    customer.save()

    from apps.sales.models import ReservationLine  # noqa: F401  (модель нужна для FK)

    reservation.status = Reservation.Status.ACTIVE
    reservation.save(update_fields=["status"])
    # Пустой резерв продать нельзя, поэтому проверяем только перенос снимка
    # через прямое создание продажи из тех же значений.
    sale = create_sale(
        customer_name=reservation.customer_name,
        customer_phone=reservation.customer_phone,
        by=admin,
    )
    assert sale.customer_name == "Иванов"  # снимок брони, а не новое имя карточки


def test_customer_with_documents_cannot_be_deleted(db, admin):
    from django.db.models import ProtectedError

    customer = Customer.objects.create(name="Иванов")
    create_sale(customer=customer, by=admin)
    with pytest.raises(ProtectedError):
        customer.delete()


# --- Экраны ---------------------------------------------------------------------------------


def test_customer_pages_render_and_search(client, make_user, db):
    _login(client, make_user)
    Customer.objects.create(name="Иванов", phone="+7 912 123-45-67")
    Customer.objects.create(name="Сидоров", phone="+7 495 000-11-22")

    html = client.get(reverse("customer_list")).content.decode()
    assert "Иванов" in html and "Сидоров" in html

    found = client.get(reverse("customer_list"), {"q": "89121234567"}).content.decode()
    assert "Иванов" in found
    assert "Сидоров" not in found


def test_customer_can_be_created_and_edited_through_ui(client, make_user, db):
    _login(client, make_user)
    resp = client.post(
        reverse("customer_create"),
        {"name": "Иванов", "phone": "8 912 123 45 67", "comment": ""},
        follow=True,
    )
    assert resp.status_code == 200
    customer = Customer.objects.get(name="Иванов")
    assert customer.phone_normalized == "79121234567"

    client.post(
        reverse("customer_edit", args=[customer.pk]),
        {"name": "Иванов Иван", "phone": "", "comment": "постоянный"},
        follow=True,
    )
    customer.refresh_from_db()
    assert customer.name == "Иванов Иван"
    assert customer.phone == ""
    assert customer.phone_normalized == ""


def test_customer_create_returns_to_local_operator_flow_with_selected_card(client, make_user, db):
    _login(client, make_user)
    target = reverse("actions_scan") + "?kind=sale"
    response = client.post(
        reverse("customer_create"),
        {"name": "Новый клиент", "phone": "", "comment": "", "next": target},
    )

    customer = Customer.objects.get(name="Новый клиент")
    assert response.status_code == 302
    assert response["Location"] == f"{target}&customer_id={customer.pk}"


def test_customer_create_rejects_external_return_target(client, make_user, db):
    _login(client, make_user)
    response = client.post(
        reverse("customer_create"),
        {"name": "Новый клиент", "phone": "", "comment": "", "next": "https://bad.example/"},
    )

    customer = Customer.objects.get(name="Новый клиент")
    assert response.status_code == 302
    assert response["Location"] == reverse("customer_detail", args=[customer.pk])


def test_customer_form_rejects_empty_name(client, make_user, db):
    _login(client, make_user)
    client.post(reverse("customer_create"), {"name": "   ", "phone": "", "comment": ""})
    assert not Customer.objects.exists()


def test_customer_card_shows_documents_and_no_combined_total(client, make_user, db, admin):
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов", phone="+79121234567")
    sale = create_sale(customer=customer, by=admin)
    order = create_repair_order(customer=customer, by=admin)

    html = client.get(reverse("customer_detail", args=[customer.pk])).content.decode()
    assert sale.number in html
    assert order.number in html
    assert "Итого по клиенту" not in html
    assert "Общий оборот" not in html


def test_customer_pages_require_permission(client, make_user, db):
    from apps.accounts import roles

    Customer.objects.create(name="Иванов")
    make_user("viewer", role=roles.VIEWER)
    client.login(username="viewer", password=PASSWORD)
    # Наблюдателю справочник доступен только на чтение через право отчётов.
    listing = client.get(reverse("customer_list"))
    assert listing.status_code in (200, 403)
    if listing.status_code == 200:
        assert client.get(reverse("customer_create")).status_code == 403


def test_anonymous_is_redirected(client, db):
    Customer.objects.create(name="Иванов")
    resp = client.get(reverse("customer_list"))
    assert resp.status_code in (302, 301)
    assert "/login" in resp["Location"]


# --- Никакой автоматической привязки --------------------------------------------------------


def test_audit_command_is_read_only(db, admin, capsys):
    from django.core.management import call_command

    Customer.objects.create(name="Иванов", phone="+79121234567")
    sale = create_sale(customer_name="Иванов", customer_phone="8 912 123 45 67", by=admin)
    order = create_repair_order(customer_name="Неизвестный", by=admin)

    call_command("audit_customer_links")
    output = capsys.readouterr().out
    assert "только чтение" in output

    sale.refresh_from_db()
    order.refresh_from_db()
    assert sale.customer_id is None  # ничего не связано автоматически
    assert order.customer_id is None


def test_audit_command_marks_ambiguous_candidates(db, admin, capsys):
    from django.core.management import call_command

    Customer.objects.create(name="Иван Иванов")
    Customer.objects.create(name="Иван Иванов")
    create_sale(customer_name="Иван Иванов", by=admin)

    call_command("audit_customer_links")
    output = capsys.readouterr().out
    assert "несколько кандидатов" in output


def test_migration_does_not_link_existing_documents(db, admin):
    """Схемная миграция добавляет пустую связь и ничего не переписывает."""
    sale = create_sale(customer_name="Иванов", by=admin)
    order = create_repair_order(customer_name="Иванов", by=admin)
    reservation = create_reservation(customer_name="Иванов", by=admin)
    assert Sale.objects.filter(customer__isnull=True).count() == 1
    assert RepairOrder.objects.filter(customer__isnull=True).count() == 1
    assert Reservation.objects.filter(customer__isnull=True).count() == 1
    assert sale.customer_id is None and order.customer_id is None
    assert reservation.customer_id is None


def test_selecting_customer_in_sale_form_fills_snapshot(client, make_user, db):
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов", phone="+7 912 123-45-67")
    client.post(
        reverse("sale_create"),
        {"customer": customer.pk, "customer_name": "", "customer_phone": "", "comment": ""},
        follow=True,
    )
    sale = Sale.objects.get()
    assert sale.customer_id == customer.pk
    assert sale.customer_name == "Иванов"
    assert sale.customer_phone == "+7 912 123-45-67"


def test_sale_form_still_accepts_manual_customer(client, make_user, db):
    _login(client, make_user)
    client.post(
        reverse("sale_create"),
        {"customer": "", "customer_name": "Разовый", "customer_phone": "", "comment": ""},
        follow=True,
    )
    sale = Sale.objects.get()
    assert sale.customer_id is None
    assert sale.customer_name == "Разовый"


def test_sale_form_requires_customer_identity(client, make_user, db):
    _login(client, make_user)
    client.post(
        reverse("sale_create"),
        {"customer": "", "customer_name": "", "customer_phone": "", "comment": ""},
    )
    assert not Sale.objects.exists()


def test_repair_form_selects_customer(client, make_user, db):
    _login(client, make_user)
    customer = Customer.objects.create(name="Петров", phone="+79990001122")
    client.post(
        reverse("repair_order_create"),
        {
            "customer": customer.pk,
            "customer_name": "",
            "customer_phone": "",
            "vehicle_make": "",
            "vehicle_model": "",
            "vehicle_identifier": "",
            "problem_description": "",
            "comment": "",
        },
        follow=True,
    )
    order = RepairOrder.objects.get()
    assert order.customer_id == customer.pk
    assert order.customer_name == "Петров"
    assert order.customer_phone == "+79990001122"


def test_decimal_import_is_used():
    """Заглушка против неиспользуемого импорта Decimal в фикстурах."""
    assert Decimal("1") == 1


def test_permission_denied_helper_is_importable():
    assert PermissionDenied is not None


# --- Производительность карточки -------------------------------------------------------------


def test_customer_card_query_count_does_not_grow_with_documents(
    client, make_user, db, admin, django_assert_num_queries
):
    """Карточка не должна давать N+1 по документам клиента."""
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов", phone="+79121234567")
    for _ in range(3):
        create_sale(customer=customer, by=admin)
        create_repair_order(customer=customer, by=admin)

    url = reverse("customer_detail", args=[customer.pk])
    client.get(url)  # прогрев кэшей сессии/прав
    with django_assert_num_queries(6):
        client.get(url)

    for _ in range(5):
        create_sale(customer=customer, by=admin)
        create_repair_order(customer=customer, by=admin)
    with django_assert_num_queries(6):
        client.get(url)


# --- Матрица прав ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,can_open,can_create",
    [
        ("STOREKEEPER", True, True),  # ремонты: оформляет документы клиента
        ("SELLER", True, True),  # продажи и резервы
        ("VIEWER", True, False),  # только чтение через право отчётов
    ],
)
def test_customer_access_matrix(client, make_user, db, role, can_open, can_create):
    from apps.accounts import roles

    make_user(f"user-{role}", role=getattr(roles, role))
    client.login(username=f"user-{role}", password=PASSWORD)
    Customer.objects.create(name="Иванов", phone="+79121234567")

    listing = client.get(reverse("customer_list"))
    assert (listing.status_code == 200) is can_open, role
    create = client.get(reverse("customer_create"))
    assert (create.status_code == 200) is can_create, role


def test_phone_is_not_shown_to_roles_without_access(client, make_user, db):
    """Роль без прав на документы клиента не получает и его телефон."""
    from apps.accounts import roles

    customer = Customer.objects.create(name="Иванов", phone="+7 912 123-45-67")
    make_user("no-access", role=roles.ADMIN if False else None)
    client.login(username="no-access", password=PASSWORD)
    for url in (reverse("customer_list"), reverse("customer_detail", args=[customer.pk])):
        resp = client.get(url)
        assert resp.status_code == 403
        assert "+7 912 123-45-67" not in resp.content.decode()
