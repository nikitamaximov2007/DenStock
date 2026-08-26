"""Сортировка отчёта «Продажи и ремонты по клиентам» по дате.

Дата строки - `last_event`: момент последнего документа клиента, попавшего в
отчёт. Это максимум из даты проведения продажи и даты завершения ремонта, то
есть та дата, которую оператор видит в колонке «Последний документ». Ни дата
создания карточки клиента, ни дата правки карточки, ни дата складского
движения сюда не входят.

Сортировка меняет ТОЛЬКО порядок строк. Суммы, состав строк и формула итога
остаются прежними.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import (
    CLIENTS_SORT_DATE,
    CLIENTS_SORT_DOCUMENTS,
    get_clients_sales_and_repairs,
    order_clients_rows,
    resolve_period,
)
from apps.sales.models import Sale
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(
                username=username, password=PASSWORD
            )
        user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


def _stock(part, location, qty, sup, admin, *, unit_cost="100"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(str(qty)), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    bolt = PartType.objects.create(
        name="Болт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("500"),
    )
    PartNumber.objects.create(part=bolt, value="700100", kind=PartNumber.Kind.OEM)
    return {
        "sup": sup, "loc": loc, "admin": admin, "bolt": bolt,
        "sale_lot": _stock(bolt, loc, 400, sup, admin),
        "repair_lot": _stock(bolt, loc, 400, sup, admin),
    }


def _sale(data, customer, *, qty=1, days_ago=0, price="500"):
    """Проведённая продажа с явной датой проведения."""
    sale = create_sale(customer_name=customer, by=data["admin"])
    add_stock_lot_to_sale(
        sale, data["sale_lot"], Decimal(str(qty)),
        unit_price=Decimal(price), by=data["admin"],
    )
    sale = complete_sale(sale, by=data["admin"])
    moment = timezone.now() - timedelta(days=days_ago)
    Sale.objects.filter(pk=sale.pk).update(sold_at=moment)
    return moment


def _repair(data, customer, *, qty=1, days_ago=0):
    """Проведённый ремонт с явной датой завершения."""
    order = create_repair_order(customer_name=customer, by=data["admin"])
    add_stock_lot_to_repair_order(
        order, data["repair_lot"], Decimal(str(qty)), by=data["admin"]
    )
    order = complete_repair_order(order, by=data["admin"])
    moment = timezone.now() - timedelta(days=days_ago)
    RepairOrder.objects.filter(pk=order.pk).update(completed_at=moment)
    return moment


def _rows(period=None, *, sort=CLIENTS_SORT_DATE, direction="desc"):
    return order_clients_rows(
        get_clients_sales_and_repairs(period or resolve_period({})),
        sort=sort, direction=direction,
    )


def _names(rows):
    return [row["display_name"] for row in rows]


def _wide_period_qs() -> str:
    """Явный широкий период: умолчание отчёта - последние 30 дней, а тесты
    расставляют документы и дальше в прошлое."""
    today = timezone.localdate()
    return (
        f"date_from={(today - timedelta(days=400)).isoformat()}"
        f"&date_to={today.isoformat()}"
    )


def _login(client, make_user, *, role=None, name="boss"):
    make_user(name, role=role, is_superuser=role is None)
    client.login(username=name, password=PASSWORD)


# --- Семантика даты ---------------------------------------------------------


def test_date_is_the_last_customer_facing_document(data):
    """Дата строки - последний документ клиента, а не первый и не карточка."""
    _sale(data, "Иванов", days_ago=10)
    last = _repair(data, "Иванов", days_ago=2)
    row = _rows()[0]
    assert row["last_event"] == last
    assert row["last_sale"] < row["last_repair"]


def test_sale_wins_when_it_is_the_later_document(data):
    _repair(data, "Иванов", days_ago=9)
    last = _sale(data, "Иванов", days_ago=1)
    assert _rows()[0]["last_event"] == last


def test_customer_card_dates_are_not_used(data):
    """Карточка клиента заведена сегодня, а документ был давно."""
    from apps.customers.models import Customer

    customer = Customer.objects.create(name="Иванов")
    assert customer.created_at.date() == timezone.localdate()
    moment = _sale(data, "Иванов", days_ago=20)
    row = _rows()[0]
    assert row["last_event"] == moment
    assert row["last_event"] != customer.created_at


# --- Порядок ----------------------------------------------------------------


def test_newest_first(data):
    _sale(data, "Старый", days_ago=20)
    _sale(data, "Средний", days_ago=10)
    _sale(data, "Новый", days_ago=1)
    assert _names(_rows(direction="desc")) == ["Новый", "Средний", "Старый"]


def test_oldest_first(data):
    _sale(data, "Старый", days_ago=20)
    _sale(data, "Средний", days_ago=10)
    _sale(data, "Новый", days_ago=1)
    assert _names(_rows(direction="asc")) == ["Старый", "Средний", "Новый"]


def test_default_order_is_untouched(data):
    """Без запроса сортировки порядок прежний: по числу документов."""
    _sale(data, "Один", days_ago=1)
    _sale(data, "Много", days_ago=20)
    _repair(data, "Много", days_ago=19)
    names = _names(_rows(sort=CLIENTS_SORT_DOCUMENTS))
    assert names == ["Много", "Один"]  # два документа впереди одного


def test_same_date_is_deterministic_in_both_directions(data):
    """Равные даты выстраиваются по алфавиту, а не произвольно."""
    moment = timezone.now() - timedelta(days=5)
    for name in ("Яковлев", "Абрамов", "Миронов"):
        sale = create_sale(customer_name=name, by=data["admin"])
        add_stock_lot_to_sale(
            sale, data["sale_lot"], Decimal("1"),
            unit_price=Decimal("500"), by=data["admin"],
        )
        sale = complete_sale(sale, by=data["admin"])
        Sale.objects.filter(pk=sale.pk).update(sold_at=moment)

    alphabetical = ["Абрамов", "Миронов", "Яковлев"]
    assert _names(_rows(direction="desc")) == alphabetical
    assert _names(_rows(direction="asc")) == alphabetical
    # Повторный вызов даёт тот же порядок: страницы не «поедут» между запросами.
    assert _names(_rows(direction="desc")) == _names(_rows(direction="desc"))


def test_rows_without_a_date_go_last_in_both_directions(data):
    """«Нет даты» это не «очень давно»: такие строки всегда в конце."""
    _sale(data, "Сдатой", days_ago=3)
    rows = _rows()
    rows.append({**rows[0], "display_name": "Бездаты", "last_event": None})
    for direction in ("desc", "asc"):
        ordered = order_clients_rows(list(rows), sort=CLIENTS_SORT_DATE, direction=direction)
        assert ordered[-1]["display_name"] == "Бездаты"


# --- Границы периода --------------------------------------------------------


def test_period_boundaries_decide_which_document_sets_the_date(data):
    """Документ вне периода дату строки не задаёт и в отчёт не попадает."""
    _sale(data, "Иванов", days_ago=40)
    inside = _sale(data, "Иванов", days_ago=3)
    today = timezone.localdate()
    period = resolve_period({
        "date_from": (today - timedelta(days=7)).isoformat(),
        "date_to": today.isoformat(),
    })
    row = _rows(period)[0]
    assert row["last_event"] == inside
    assert row["sale_count"] == 1  # старая продажа за границей периода


def test_document_on_the_first_day_of_the_period_counts(data):
    moment = _sale(data, "Иванов", days_ago=7)
    today = timezone.localdate()
    period = resolve_period({
        "date_from": (today - timedelta(days=7)).isoformat(),
        "date_to": today.isoformat(),
    })
    rows = _rows(period)
    assert len(rows) == 1
    assert rows[0]["last_event"] == moment


def test_sorting_works_together_with_a_narrow_period(data):
    _sale(data, "Старый", days_ago=6)
    _sale(data, "Новый", days_ago=2)
    _sale(data, "Вне периода", days_ago=30)
    today = timezone.localdate()
    period = resolve_period({
        "date_from": (today - timedelta(days=7)).isoformat(),
        "date_to": today.isoformat(),
    })
    assert _names(_rows(period, direction="desc")) == ["Новый", "Старый"]
    assert _names(_rows(period, direction="asc")) == ["Старый", "Новый"]


# --- Суммы не меняются ------------------------------------------------------


def test_totals_are_identical_whatever_the_order(data):
    _sale(data, "Иванов", qty=2, days_ago=10)
    _repair(data, "Иванов", qty=3, days_ago=2)
    _sale(data, "Петров", qty=1, days_ago=5)

    def totals(rows):
        return {
            row["display_name"]: (
                row["revenue"], row["repair_customer_amount"],
                row["client_total_known"], row["issued_cost"],
            )
            for row in rows
        }

    default = totals(_rows(sort=CLIENTS_SORT_DOCUMENTS))
    assert totals(_rows(direction="desc")) == default
    assert totals(_rows(direction="asc")) == default


def test_client_total_formula_survives_sorting(data):
    _sale(data, "Иванов", qty=2, days_ago=4)
    _repair(data, "Иванов", qty=3, days_ago=1)
    row = _rows(direction="desc")[0]
    assert row["client_total_known"] == row["revenue"] + row["repair_customer_amount"]
    # Себестоимость склада в итог не входит и работы тоже.
    assert row["client_total_known"] != row["revenue"] + row["issued_cost"]


def test_row_membership_does_not_change(data):
    _sale(data, "Иванов", days_ago=10)
    _repair(data, "Петров", days_ago=2)
    _sale(data, "Сидоров", days_ago=5)
    default = sorted(_names(_rows(sort=CLIENTS_SORT_DOCUMENTS)))
    assert sorted(_names(_rows(direction="desc"))) == default
    assert sorted(_names(_rows(direction="asc"))) == default


# --- Экран, страницы, права -------------------------------------------------


def test_screen_orders_newest_and_oldest(client, data, make_user):
    _sale(data, "Старый", days_ago=20)
    _sale(data, "Новый", days_ago=1)
    _login(client, make_user)
    url = reverse("reports_clients_overview")

    html = client.get(f"{url}?sort=date&direction=desc").content.decode()
    assert html.index("Новый") < html.index("Старый")
    assert "сначала новые" in html

    html = client.get(f"{url}?sort=date&direction=asc").content.decode()
    assert html.index("Старый") < html.index("Новый")
    assert "сначала старые" in html


def test_screen_default_order_needs_no_parameters(client, data, make_user):
    _sale(data, "Один", days_ago=1)
    _sale(data, "Много", days_ago=20)
    _repair(data, "Много", days_ago=19)
    _login(client, make_user)
    html = client.get(reverse("reports_clients_overview")).content.decode()
    assert html.index("Много") < html.index("Один")
    assert "по числу документов" in html


def test_broken_sort_parameters_fall_back_to_default(client, data, make_user):
    _sale(data, "Один", days_ago=1)
    _sale(data, "Много", days_ago=20)
    _repair(data, "Много", days_ago=19)
    _login(client, make_user)
    url = reverse("reports_clients_overview")
    for query in ("?sort=drop%20table&direction=desc", "?sort=date&direction=вбок",
                  "?sort=&direction="):
        html = client.get(url + query).content.decode()
        assert html.index("Много") < html.index("Один"), query
        assert "по числу документов" in html


def test_pagination_continues_the_same_order(client, data, make_user):
    from apps.reports.views import _CLIENT_REPORT_PAGE_SIZE

    total = _CLIENT_REPORT_PAGE_SIZE + 5
    for index in range(total):
        # Чем больше индекс, тем старше документ.
        _sale(data, f"Клиент-{index:03d}", days_ago=index + 1)
    _login(client, make_user)
    base = f"{reverse('reports_clients_overview')}?{_wide_period_qs()}&sort=date"

    first = client.get(f"{base}&direction=desc").context["page_obj"]
    second = client.get(f"{base}&direction=desc&page=2").context["page_obj"]
    order = [row["display_name"] for row in first.object_list] + [
        row["display_name"] for row in second.object_list
    ]
    assert order == sorted(order)  # Клиент-000 новее всех, дальше по возрастанию
    assert len(order) == total
    assert len(set(order)) == total  # ни одна строка не потерялась и не задвоилась
    assert len(first.object_list) == _CLIENT_REPORT_PAGE_SIZE

    ascending = client.get(f"{base}&direction=asc").context["page_obj"]
    assert [row["display_name"] for row in ascending.object_list] == sorted(
        order, reverse=True
    )[:_CLIENT_REPORT_PAGE_SIZE]


def _query_count(client, url) -> int:
    with CaptureQueriesContext(connection) as captured:
        assert client.get(url).status_code == 200
    return len(captured.captured_queries)


def test_sorting_does_not_add_queries(client, data, make_user):
    """Порядок строк считается в памяти по уже полученным агрегатам."""
    for index in range(30):
        _sale(data, f"Клиент-{index:03d}", days_ago=index + 1)
    _login(client, make_user)
    base = f"{reverse('reports_clients_overview')}?{_wide_period_qs()}"

    baseline = _query_count(client, base)
    assert _query_count(client, f"{base}&sort=date&direction=desc") == baseline
    assert _query_count(client, f"{base}&sort=date&direction=asc") == baseline


def test_more_clients_do_not_add_queries(client, data, make_user):
    """Число запросов не зависит от числа строк: N+1 нет."""
    _login(client, make_user)
    url = f"{reverse('reports_clients_overview')}?{_wide_period_qs()}&sort=date&direction=desc"
    for index in range(5):
        _sale(data, f"Малый-{index:03d}", days_ago=index + 1)
    small = _query_count(client, url)
    for index in range(40):
        _sale(data, f"Большой-{index:03d}", days_ago=index + 1)
    assert _query_count(client, url) == small


def test_sorted_report_needs_the_reports_right(client, data, make_user):
    _sale(data, "Иванов", days_ago=1)
    url = f"{reverse('reports_clients_overview')}?sort=date&direction=desc"
    assert "login" in client.get(url).url  # без входа

    make_user("seller", role=roles.SELLER)
    client.login(username="seller", password=PASSWORD)
    assert client.get(url).status_code == 403

    client.logout()
    make_user("watcher", role=roles.VIEWER)
    client.login(username="watcher", password=PASSWORD)
    assert client.get(url).status_code == 200  # наблюдателю отчёты открыты
