"""Телефон клиента: необязательное поле и безопасный поиск.

Что здесь гарантируется:

* телефон остаётся свободным текстом и необязательным: пустой телефон это
  нормальный документ, а введённая запись показывается ровно как ввели;
* поиск находит документ по «+7», по «8» и по записи без разделителей, потому
  что рядом хранится нормализованная форма только для поиска;
* уникальности у телефона нет: один номер законно встречается в разных
  документах и у разных клиентов;
* нормализация не ломает чужие форматы: то, что не похоже на российский номер,
  остаётся набором своих цифр.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.core.phones import customer_search_q, looks_like_phone, normalize_phone
from apps.repairs.models import RepairOrder
from apps.repairs.services import create_repair_order
from apps.sales.models import Sale
from apps.sales.services import create_reservation, create_sale

PASSWORD = "parol-12345"


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


def _login(client, make_user, *, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


# --- Нормализация ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "+7 916 123-45-67",
        "8 (916) 123-45-67",
        "8-916-123-45-67",
        "79161234567",
        "89161234567",
        "9161234567",
        "  +7(916)1234567  ",
    ],
)
def test_russian_variants_normalize_to_one_form(raw):
    assert normalize_phone(raw) == "79161234567"


def test_empty_phone_stays_empty():
    assert normalize_phone("") == ""
    assert normalize_phone("   ") == ""
    assert normalize_phone(None) == ""
    assert normalize_phone("без телефона") == ""


def test_foreign_number_keeps_its_own_digits():
    """Чужие форматы не угадываем: остаются свои цифры без выдуманной семёрки."""
    assert normalize_phone("+1 202 555 0134") == "12025550134"
    assert normalize_phone("+49 30 123456") == "4930123456"
    # Городской номер без кода страны тоже не получает выдуманную семёрку.
    assert normalize_phone("495 000 11 22") == "4950001122"


def test_looks_like_phone_rejects_names():
    assert looks_like_phone("+7 916 123-45-67")
    assert looks_like_phone("9161234567")
    assert not looks_like_phone("Иванов")
    assert not looks_like_phone("")
    assert not looks_like_phone("12")


# --- Поле необязательное и обратно совместимое --------------------------------------------


def test_phone_is_optional_everywhere(db, admin):
    sale = create_sale(customer_name="Иванов", by=admin)
    order = create_repair_order(customer_name="Иванов", by=admin)
    reservation = create_reservation(customer_name="Иванов", by=admin)
    for document in (sale, order, reservation):
        assert document.customer_phone == ""
        assert document.customer_phone_normalized == ""


def test_visible_phone_is_kept_exactly_as_entered(db, admin):
    sale = create_sale(customer_name="Иванов", customer_phone="+7 (916) 123-45-67", by=admin)
    sale.refresh_from_db()
    assert sale.customer_phone == "+7 (916) 123-45-67"
    assert sale.customer_phone_normalized == "79161234567"


def test_same_phone_allowed_in_many_documents(db, admin):
    """Уникальности нет и не должно быть: номер живёт в разных документах."""
    first = create_sale(customer_name="Иванов", customer_phone="+79161234567", by=admin)
    second = create_sale(customer_name="Петров", customer_phone="8 916 123 45 67", by=admin)
    order = create_repair_order(customer_name="Иванов", customer_phone="89161234567", by=admin)
    assert first.customer_phone_normalized == second.customer_phone_normalized
    assert order.customer_phone_normalized == first.customer_phone_normalized


def test_normalized_phone_follows_update_fields(db, admin):
    """Смена телефона через update_fields не оставляет старую поисковую форму."""
    sale = create_sale(customer_name="Иванов", customer_phone="+79161234567", by=admin)
    sale.customer_phone = "8 495 000 11 22"
    sale.save(update_fields=["customer_phone"])
    sale.refresh_from_db()
    assert sale.customer_phone_normalized == "74950001122"


# --- Поиск -------------------------------------------------------------------------------


def test_search_finds_document_by_any_phone_format(db, admin):
    sale = create_sale(customer_name="Иванов", customer_phone="+7 (916) 123-45-67", by=admin)
    for query in ("+7 916 123-45-67", "89161234567", "9161234567", "79161234567"):
        found = Sale.objects.filter(customer_search_q(query))
        assert list(found) == [sale], query


def test_search_finds_document_by_phone_tail(db, admin):
    sale = create_sale(customer_name="Иванов", customer_phone="+79161234567", by=admin)
    assert list(Sale.objects.filter(customer_search_q("1234567"))) == [sale]


def test_search_by_name_still_works(db, admin):
    # Регистронезависимость кириллицы даёт PostgreSQL (ILIKE); на SQLite,
    # где идут тесты, LIKE регистронезависим только для латиницы, поэтому
    # здесь проверяем подстроку в исходном регистре.
    sale = create_sale(customer_name="Иванов Пётр", by=admin)
    create_sale(customer_name="Сидоров", by=admin)
    assert list(Sale.objects.filter(customer_search_q("Иванов"))) == [sale]


def test_search_by_name_does_not_match_phone_column(db, admin):
    create_sale(customer_name="Иванов", customer_phone="+79161234567", by=admin)
    assert not Sale.objects.filter(customer_search_q("Сидоров")).exists()


def test_empty_query_does_not_filter(db, admin):
    create_sale(customer_name="Иванов", by=admin)
    assert Sale.objects.filter(customer_search_q("")).count() == 1


def test_repair_orders_are_searchable_by_phone(db, admin):
    order = create_repair_order(customer_name="Иванов", customer_phone="8 916 123-45-67", by=admin)
    assert list(RepairOrder.objects.filter(customer_search_q("+79161234567"))) == [order]


# --- Экраны ------------------------------------------------------------------------------


def test_sale_list_search_by_phone(client, make_user, db, admin):
    _login(client, make_user)
    create_sale(customer_name="Иванов", customer_phone="+7 916 123-45-67", by=admin)
    create_sale(customer_name="Сидоров", customer_phone="+7 495 000-11-22", by=admin)
    html = client.get(reverse("sale_list"), {"q": "89161234567"}).content.decode()
    assert "Иванов" in html
    assert "Сидоров" not in html


def test_repair_list_search_by_phone(client, make_user, db, admin):
    _login(client, make_user)
    create_repair_order(customer_name="Иванов", customer_phone="89161234567", by=admin)
    create_repair_order(customer_name="Сидоров", customer_phone="84950001122", by=admin)
    html = client.get(reverse("repair_order_list"), {"q": "+7 916 123 45 67"}).content.decode()
    assert "Иванов" in html
    assert "Сидоров" not in html


def test_sale_list_shows_phone_column(client, make_user, db, admin):
    _login(client, make_user)
    create_sale(customer_name="Иванов", customer_phone="+7 916 123-45-67", by=admin)
    html = client.get(reverse("sale_list")).content.decode()
    assert "Телефон" in html
    assert "+7 916 123-45-67" in html


def test_lists_without_query_show_everything(client, make_user, db, admin):
    _login(client, make_user)
    create_sale(customer_name="Иванов", by=admin)
    create_sale(customer_name="Сидоров", customer_phone="+79161234567", by=admin)
    html = client.get(reverse("sale_list")).content.decode()
    assert "Иванов" in html
    assert "Сидоров" in html


# --- Отчёт по клиентам ---------------------------------------------------------------------


def test_customer_report_lists_phones_seen_in_period(db, admin):
    from apps.reports.services import get_customer_phones, resolve_period

    sale = create_sale(customer_name="Иванов", customer_phone="+7 916 123-45-67", by=admin)
    sale.status = Sale.Status.COMPLETED
    sale.revenue_total = Decimal("100")
    from django.utils import timezone

    sale.sold_at = timezone.now()
    sale.save(update_fields=["status", "revenue_total", "sold_at"])

    period = resolve_period({})
    phones = get_customer_phones(period, customer_name="Иванов", missing=False)
    assert phones == ["+7 916 123-45-67"]
