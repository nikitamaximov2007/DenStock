"""Stage 6 — клиент с самой свежей операцией стоит первым.

Сотрудник почти всегда оформляет документ на того, кто уже приходил, а список
был алфавитным на несколько сотен строк. Теперь порядок следует за работой.

Наверх поднимает только ЗАВЕРШЁННЫЙ документ: черновик ничего не выдал, а
отменённая продажа товар клиенту не оставила. Клиенты без истории идут ниже
всех, между собой - по имени, чтобы список не менялся от запроса к запросу.
"""
import datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.customers.services import customers_by_recent_activity
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale

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


@pytest.fixture
def env(db, admin):
    """Минимальный остаток: корзина нужна, чтобы форма выбора клиента вообще была."""
    from apps.actions.models import PartCustomsInfo
    from apps.catalog.models import Category, PartNumber, PartType, Unit
    from apps.inventory.services import create_stock_lot, receive_stock_lot
    from apps.procurement.models import Batch, BatchLine
    from apps.procurement.services import finalize_cost
    from apps.suppliers.models import Supplier
    from apps.warehouse.models import StorageLocation

    sup = Supplier.objects.create(name="ООО Поставка")
    loc = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D03-C08", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Болт", category=Category.objects.create(name="Вариатор"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value="700100", kind=PartNumber.Kind.OEM)
    PartCustomsInfo.objects.create(
        part_type=part, gross_weight_kg=Decimal("0.350"), net_weight_kg=Decimal("0.300"),
        application_area=PartCustomsInfo.ApplicationArea.SNOWMOBILE,
    )
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("10"), unit_cost_currency=Decimal("1")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    receive_stock_lot(create_stock_lot(line, loc, Decimal("10")), by=admin)
    return {"admin": admin, "loc": loc, "part": part}


def _customer(name, phone=""):
    return Customer.objects.create(name=name, phone=phone)


def _ago(days):
    return timezone.now() - datetime.timedelta(days=days)


def _sale(customer, *, days_ago, status=Sale.Status.COMPLETED):
    sale = Sale.objects.create(
        customer=customer, customer_name=customer.name, status=status,
        sold_at=_ago(days_ago) if status == Sale.Status.COMPLETED else None,
    )
    Sale.objects.filter(pk=sale.pk).update(sold_at=_ago(days_ago))
    return sale


def _repair(customer, *, days_ago, status=RepairOrder.Status.COMPLETED):
    order = RepairOrder.objects.create(
        customer=customer, customer_name=customer.name, status=status,
    )
    RepairOrder.objects.filter(pk=order.pk).update(completed_at=_ago(days_ago))
    return order


def _names(queryset):
    return [customer.name for customer in queryset]


# --- Порядок -----------------------------------------------------------------------------


def test_most_recent_sale_comes_first(db):
    old = _customer("Аистов")
    fresh = _customer("Яковлев")
    _sale(old, days_ago=30)
    _sale(fresh, days_ago=1)
    assert _names(customers_by_recent_activity())[:2] == ["Яковлев", "Аистов"]


def test_order_is_descending_by_last_activity(db):
    a, b, c = _customer("Первый"), _customer("Второй"), _customer("Третий")
    _sale(a, days_ago=20)
    _sale(b, days_ago=10)
    _sale(c, days_ago=1)
    assert _names(customers_by_recent_activity())[:3] == ["Третий", "Второй", "Первый"]


def test_a_repair_counts_as_activity_too(db):
    sold = _customer("Продажа")
    repaired = _customer("Ремонт")
    _sale(sold, days_ago=5)
    _repair(repaired, days_ago=1)
    assert _names(customers_by_recent_activity())[:2] == ["Ремонт", "Продажа"]


def test_the_latest_of_sale_and_repair_wins(db):
    customer = _customer("Иванов")
    other = _customer("Петров")
    _sale(customer, days_ago=40)
    _repair(customer, days_ago=1)
    _sale(other, days_ago=10)
    assert _names(customers_by_recent_activity())[:2] == ["Иванов", "Петров"]


def test_customers_without_history_go_below(db):
    active = _customer("Яковлев")
    _sale(active, days_ago=90)
    _customer("Аистов")
    _customer("Борисов")
    assert _names(customers_by_recent_activity()) == ["Яковлев", "Аистов", "Борисов"]


def test_customers_without_history_keep_alphabetical_order(db):
    _customer("Сидоров")
    _customer("Абрамов")
    _customer("Миронов")
    assert _names(customers_by_recent_activity()) == ["Абрамов", "Миронов", "Сидоров"]


# --- Отменённое и черновое наверх не поднимают -------------------------------------------


def test_a_cancelled_sale_does_not_lift_the_customer(db):
    cancelled = _customer("Отменённый")
    real = _customer("Настоящий")
    _sale(cancelled, days_ago=1, status=Sale.Status.CANCELED)
    _sale(real, days_ago=20)
    assert _names(customers_by_recent_activity())[:2] == ["Настоящий", "Отменённый"]


def test_a_draft_sale_does_not_lift_the_customer(db):
    draft = _customer("Черновик")
    real = _customer("Настоящий")
    _sale(draft, days_ago=1, status=Sale.Status.DRAFT)
    _sale(real, days_ago=20)
    assert _names(customers_by_recent_activity())[:2] == ["Настоящий", "Черновик"]


def test_a_cancelled_repair_does_not_lift_the_customer(db):
    cancelled = _customer("Отменённый")
    real = _customer("Настоящий")
    _repair(cancelled, days_ago=1, status=RepairOrder.Status.CANCELED)
    _repair(real, days_ago=20)
    assert _names(customers_by_recent_activity())[:2] == ["Настоящий", "Отменённый"]


# --- Устойчивость ------------------------------------------------------------------------


def test_equal_dates_break_ties_deterministically(db):
    same = _ago(3)
    for name in ("Волков", "Абрамов", "Миронов"):
        customer = _customer(name)
        sale = _sale(customer, days_ago=3)
        Sale.objects.filter(pk=sale.pk).update(sold_at=same)
    first = _names(customers_by_recent_activity())
    assert first == ["Абрамов", "Волков", "Миронов"]
    assert _names(customers_by_recent_activity()) == first


def test_limit_is_respected(db):
    for index in range(5):
        _customer(f"Клиент {index}")
    assert len(list(customers_by_recent_activity(limit=3))) == 3


def test_unlimited_queryset_stays_filterable_for_form_validation(db):
    customer = _customer("Иванов")
    queryset = customers_by_recent_activity(limit=None)
    assert queryset.get(pk=customer.pk) == customer


# --- Экраны ------------------------------------------------------------------------------


def _scan_page_with_a_cart(client, make_user, env):
    """Выбор клиента живёт в форме проведения, а она есть только у корзины."""
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)
    client.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": "sale", "quantity": "1", "q": "700100",
    })
    return client.get(reverse("actions_scan")).content.decode()


def test_quick_actions_lists_the_recent_customer_first(client, make_user, env):
    old = _customer("Аистов", phone="+7 912 000-00-01")
    fresh = _customer("Яковлев", phone="+7 912 000-00-02")
    _sale(old, days_ago=30)
    _sale(fresh, days_ago=1)
    html = _scan_page_with_a_cart(client, make_user, env)
    assert html.index("Яковлев") < html.index("Аистов")


def test_quick_actions_keeps_name_and_phone_search_data(client, make_user, env):
    _customer("Яковлев", phone="+7 912 000-00-02")
    html = _scan_page_with_a_cart(client, make_user, env)
    assert 'data-customer-search="яковлев +7 912 000-00-02"' in html


def test_sale_form_lists_the_recent_customer_first(db):
    from apps.sales.forms import SaleForm

    old = _customer("Аистов")
    fresh = _customer("Яковлев")
    _sale(old, days_ago=30)
    _sale(fresh, days_ago=1)
    choices = [label for _value, label in SaleForm().fields["customer"].choices if _value]
    assert choices[:2] == ["Яковлев", "Аистов"]


def test_repair_form_lists_the_recent_customer_first(db):
    from apps.repairs.forms import RepairOrderForm

    old = _customer("Аистов")
    fresh = _customer("Яковлев")
    _repair(old, days_ago=30)
    _repair(fresh, days_ago=1)
    choices = [label for _value, label in RepairOrderForm().fields["customer"].choices if _value]
    assert choices[:2] == ["Яковлев", "Аистов"]
