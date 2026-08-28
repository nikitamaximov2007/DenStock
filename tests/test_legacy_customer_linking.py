"""Ручная привязка исторических клиентов из отчёта.

Автоматический backfill на этих данных находит ноль безопасных групп: у старых
документов есть имя и почти нигде нет телефона, а связывать по одному имени
нельзя. Здесь закреплён путь, где решение принимает человек: отчёт показывает
«Без карточки», оператор открывает форму, правит подсказанные имя и телефон и
подтверждает. Связываются документы ровно этой записи, снимки в документах
остаются нетронутыми.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.catalog.models import Category, PartType, Unit
from apps.customers.legacy_linking import (
    legacy_group,
    legacy_group_summary,
    link_legacy_group,
    suggest_identity,
)
from apps.customers.models import Customer
from apps.customers.services import search_customers
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="hozyain", password=PASSWORD)


@pytest.fixture
def boss(client, admin):
    client.login(username="hozyain", password=PASSWORD)
    return client


def _sale(name, *, status=Sale.Status.COMPLETED, customer=None, phone=""):
    return Sale.objects.create(
        status=status, customer_name=name, customer_phone=phone, customer=customer
    )


def _repair(name, *, status=RepairOrder.Status.COMPLETED, customer=None):
    return RepairOrder.objects.create(status=status, customer_name=name, customer=customer)



@pytest.fixture
def stock(db, admin):
    """Настоящий остаток: без строк документа отчёт по клиентам пуст."""
    supplier = Supplier.objects.create(name="ООО Поставка")
    location = StorageLocation.objects.create(
        name="Ячейка", code="L-01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="БОЛТ", category=Category.objects.create(name="Крепёж"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("500"),
    )
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("50"), unit_cost_currency=Decimal("100")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("50"))
    receive_stock_lot(lot, by=admin)
    return {"admin": admin, "lot": lot, "part": part}


def _sold(stock, *, name="", customer=None, quantity="1"):
    sale = create_sale(customer_name=name, customer=customer, by=stock["admin"])
    add_stock_lot_to_sale(
        sale, stock["lot"], Decimal(quantity), unit_price=Decimal("500"), by=stock["admin"]
    )
    return complete_sale(sale, by=stock["admin"])


# --- Подсказка имени и телефона ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "name", "phone"),
    [
        ("Петр 89120707078", "Петр", "89120707078"),
        ("Захаров Андрей, Искитим, 89134535122", "Захаров Андрей, Искитим", "89134535122"),
        ("Сидоров +7 (912) 070-70-78", "Сидоров", "+7 (912) 070-70-78"),
        ("Иванов 8 912 070 70 78", "Иванов", "8 912 070 70 78"),
    ],
)
def test_an_obvious_phone_is_offered_separately(raw, name, phone):
    suggestion = suggest_identity(raw)
    assert suggestion["name"] == name
    assert suggestion["phone"] == phone


@pytest.mark.parametrize(
    "raw", ["Зуев Егор", "ООО Ромашка 2026", "Дом 12 квартира 5", "Клиент 4930123456"]
)
def test_an_unclear_record_keeps_the_whole_name(raw):
    """Выдуманный телефон хуже пустого: не уверены - не подставляем."""
    suggestion = suggest_identity(raw)
    assert suggestion["name"] == raw
    assert suggestion["phone"] == ""


# --- Группа документов -------------------------------------------------------------


def test_the_group_covers_only_its_own_unlinked_completed_documents(db):
    mine_sale = _sale("Петр 89120707078")
    mine_repair = _repair("Петр 89120707078")
    other = _sale("Зуев Егор")
    draft = _sale("Петр 89120707078", status=Sale.Status.DRAFT)
    already = _sale("Петр 89120707078", customer=Customer.objects.create(name="Пётр"))

    sales, repairs = legacy_group("Петр 89120707078")

    assert set(sales.values_list("pk", flat=True)) == {mine_sale.pk}
    assert set(repairs.values_list("pk", flat=True)) == {mine_repair.pk}
    assert other.pk not in set(sales.values_list("pk", flat=True))
    assert draft.pk not in set(sales.values_list("pk", flat=True))
    assert already.pk not in set(sales.values_list("pk", flat=True))
    assert legacy_group_summary("Петр 89120707078") == {
        "name": "Петр 89120707078", "sales": 1, "repairs": 1
    }


def test_linking_touches_only_the_chosen_group(db):
    first = _sale("Петр 89120707078")
    second = _repair("Петр 89120707078")
    stranger = _sale("Зуев Егор")
    customer = Customer.objects.create(name="Пётр", phone="89120707078")

    result = link_legacy_group(legacy_name="Петр 89120707078", customer=customer)

    first.refresh_from_db()
    second.refresh_from_db()
    stranger.refresh_from_db()
    assert (first.customer_id, second.customer_id) == (customer.pk, customer.pk)
    assert stranger.customer_id is None  # чужая запись не тронута
    assert (result["sales_linked"], result["repairs_linked"]) == (1, 1)


def test_the_document_snapshots_survive_linking(db):
    sale = _sale("Петр 89120707078", phone="старый телефон")
    sale.revenue_total = Decimal("1234")
    sale.save(update_fields=["revenue_total"])
    before = (sale.customer_name, sale.customer_phone, sale.revenue_total, sale.sold_at)
    customer = Customer.objects.create(name="Совсем другое имя", phone="+79990000000")

    link_legacy_group(legacy_name="Петр 89120707078", customer=customer)

    sale.refresh_from_db()
    assert (
        sale.customer_name, sale.customer_phone, sale.revenue_total, sale.sold_at
    ) == before
    assert sale.customer_id == customer.pk  # изменилась только ссылка


def test_linking_twice_changes_nothing_the_second_time(db):
    sale = _sale("Петр 89120707078")
    customer = Customer.objects.create(name="Пётр")

    first = link_legacy_group(legacy_name="Петр 89120707078", customer=customer)
    second = link_legacy_group(legacy_name="Петр 89120707078", customer=customer)

    sale.refresh_from_db()
    assert sale.customer_id == customer.pk
    assert first["sales_linked"] == 1
    assert second["sales_linked"] == 0  # группа уже пуста
    assert Customer.objects.count() == 1


def test_an_empty_group_is_refused(db):
    customer = Customer.objects.create(name="Пётр")
    with pytest.raises(ValueError):
        link_legacy_group(legacy_name="   ", customer=customer)


# --- Экран ---------------------------------------------------------------------------


def test_the_report_offers_a_card_for_an_unlinked_group(boss, stock):
    _sold(stock, name="Зуев Егор")
    _sold(stock, customer=Customer.objects.create(name="Иванов"))

    body = boss.get(reverse("reports_clients_overview")).content.decode()

    assert "Без карточки" in body
    assert "Создать карточку" in body
    assert "Карточка есть" in body
    assert "Открыть карточку" in body
    assert reverse("legacy_customer_link") in body


def test_the_form_shows_the_original_record_and_the_suggestion(boss, db):
    _sale("Петр 89120707078")
    _repair("Петр 89120707078")

    body = boss.get(
        reverse("legacy_customer_link"), {"legacy_name": "Петр 89120707078"}
    ).content.decode()

    assert "Петр 89120707078" in body  # исходная запись показана целиком
    assert 'value="Петр"' in body  # подсказка имени
    assert 'value="89120707078"' in body  # подсказка телефона
    assert "Описание клиента" in body
    assert "продаж 1" in body and "ремонтов 1" in body


def test_creating_a_card_from_the_form_links_the_group(boss, db):
    sale = _sale("Петр 89120707078")
    repair = _repair("Петр 89120707078")

    boss.post(
        reverse("legacy_customer_link"),
        {
            "legacy_name": "Петр 89120707078",
            "name": "Пётр Иванов",
            "phone": "89120707078",
            "comment": "Снегоход, приезжает по субботам",
        },
        follow=True,
    )

    customer = Customer.objects.get()
    sale.refresh_from_db()
    repair.refresh_from_db()
    assert customer.name == "Пётр Иванов"
    assert customer.comment == "Снегоход, приезжает по субботам"
    assert (sale.customer_id, repair.customer_id) == (customer.pk, customer.pk)
    assert sale.customer_name == "Петр 89120707078"  # снимок не переписан


def test_a_name_only_group_may_be_confirmed_without_a_phone(boss, db):
    """«Зуев Егор» без телефона: вручную подтвердить можно, автоматически нет."""
    sale = _sale("Зуев Егор")

    boss.post(
        reverse("legacy_customer_link"),
        {"legacy_name": "Зуев Егор", "name": "Зуев Егор", "phone": "", "comment": ""},
        follow=True,
    )

    customer = Customer.objects.get()
    sale.refresh_from_db()
    assert customer.phone == ""
    assert sale.customer_id == customer.pk


def test_an_existing_card_can_be_chosen_instead(boss, db):
    sale = _sale("Петр 89120707078")
    existing = Customer.objects.create(name="Пётр Иванов", phone="89120707078")

    boss.post(
        reverse("legacy_customer_link"),
        {"legacy_name": "Петр 89120707078", "existing_customer": str(existing.pk)},
        follow=True,
    )

    sale.refresh_from_db()
    assert sale.customer_id == existing.pk
    assert Customer.objects.count() == 1  # дубля не появилось


def test_the_search_never_picks_a_card_by_itself(boss, db):
    _sale("Петр 89120707078")
    Customer.objects.create(name="Пётр Иванов", phone="89120707078")
    Customer.objects.create(name="Пётр Сидоров", phone="89120707078")

    body = boss.get(
        reverse("legacy_customer_link"),
        {"legacy_name": "Петр 89120707078", "q": "Пётр"},
    ).content.decode()

    assert "Пётр Иванов" in body and "Пётр Сидоров" in body
    assert Sale.objects.filter(customer__isnull=False).count() == 0  # ничего не связано


def test_a_missing_group_is_refused_by_the_screen(boss, db):
    response = boss.get(
        reverse("legacy_customer_link"), {"legacy_name": "Такого клиента нет"}, follow=True
    )
    assert "не найдено" in response.content.decode()
    assert Customer.objects.count() == 0


# --- Справочник и быстрые действия ----------------------------------------------------


def test_the_linked_customer_is_findable_by_name_and_phone(db):
    _sale("Петр 89120707078")
    customer = Customer.objects.create(name="Пётр Иванов", phone="89120707078")
    link_legacy_group(legacy_name="Петр 89120707078", customer=customer)

    assert [found.pk for found in search_customers("Иванов")] == [customer.pk]
    assert [found.pk for found in search_customers("9120707078")] == [customer.pk]


@pytest.mark.parametrize("kind", ["sale", "repair"])
def test_the_quick_action_screen_is_given_the_linked_customer(boss, db, kind):
    _sale("Петр 89120707078")
    customer = Customer.objects.create(name="Пётр Иванов", phone="89120707078")
    link_legacy_group(legacy_name="Петр 89120707078", customer=customer)

    response = boss.get(reverse("actions_scan"), {"kind": kind})

    assert customer.pk in {found.pk for found in response.context["customers"]}


def test_the_customer_card_shows_the_linked_history(boss, db):
    sale = _sale("Петр 89120707078")
    customer = Customer.objects.create(name="Пётр Иванов")
    link_legacy_group(legacy_name="Петр 89120707078", customer=customer)

    body = boss.get(reverse("customer_detail", args=[customer.pk])).content.decode()

    assert sale.number in body


def test_the_description_stays_out_of_lists_and_selectors(boss, db):
    _sale("Петр 89120707078")
    customer = Customer.objects.create(
        name="Пётр Иванов", phone="89120707078", comment="Личная заметка про долг"
    )
    link_legacy_group(legacy_name="Петр 89120707078", customer=customer)

    card = boss.get(reverse("customer_detail", args=[customer.pk])).content.decode()
    assert "Личная заметка про долг" in card

    for url, params in (
        (reverse("customer_list"), {}),
        (reverse("reports_clients_overview"), {}),
        (reverse("actions_scan"), {"kind": "sale"}),
        (reverse("actions_scan"), {"kind": "repair"}),
        (reverse("part_search"), {"q": "Пётр"}),
    ):
        body = boss.get(url, params).content.decode()
        assert "Личная заметка про долг" not in body, url
