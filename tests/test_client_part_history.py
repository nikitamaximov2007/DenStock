"""Клиентская история: что именно получил клиент, когда и на какую сумму.

Раньше нажатие на клиента приводило к списку документов, и чтобы увидеть
детали, нужно было открыть ещё и продажу. Для ежедневной работы этот уровень
лишний: вопрос звучит «что мы продавали этому клиенту», а номер документа на
него не отвечает.

Здесь закреплено новое поведение и то, что упрощение не стёрло данные:
себестоимость, прибыль и источник остатка убраны с клиентского экрана, но
остались в самих документах и в складской истории.
"""
import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import get_client_part_history, resolve_period
from apps.sales.models import Sale
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
UNIT_PRICE = Decimal("500")


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
    return make_user("boss", is_superuser=True)


def _lot(part, location, qty, sup, admin, *, unit_cost="100"):
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
    parts = {}
    lots = {}
    for key, name, number in (
        ("bolt", "Болт", "700100"),
        ("belt", "Ремень", "700200"),
        ("filter", "Фильтр", "700300"),
        ("gasket", "Прокладка", "700400"),
    ):
        part = PartType.objects.create(
            name=name, category=cat, unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=UNIT_PRICE,
        )
        PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM)
        parts[key] = part
        lots[key] = _lot(part, loc, 400, sup, admin)
    return {"sup": sup, "loc": loc, "admin": admin, "parts": parts, "lots": lots}


def _sale(data, *, customer=None, name="", items=(("bolt", 1),), price=UNIT_PRICE):
    sale = create_sale(customer=customer, customer_name=name, by=data["admin"])
    for key, qty in items:
        add_stock_lot_to_sale(
            sale, data["lots"][key], Decimal(str(qty)), unit_price=price, by=data["admin"]
        )
    return complete_sale(sale, by=data["admin"])


def _repair(data, *, customer=None, name="", items=(("belt", 1),)):
    order = create_repair_order(customer=customer, customer_name=name, by=data["admin"])
    for key, qty in items:
        add_stock_lot_to_repair_order(
            order, data["lots"][key], Decimal(str(qty)), by=data["admin"]
        )
    return complete_repair_order(order, by=data["admin"])


def _login(client, user):
    client.login(username=user.username, password=PASSWORD)


def _sales_detail(client, customer):
    return client.get(reverse("reports_sales_by_client_detail"), {"customer_id": customer.pk})


def _repairs_detail(client, customer):
    return client.get(reverse("reports_repairs_by_client_detail"), {"customer_id": customer.pk})


# --- A: строки деталей, а не документы ----------------------------------------------------


def test_sales_detail_shows_part_lines_not_documents(client, data, admin):
    """Три продажи на семь строк дают семь строк деталей, а не три документа."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("bolt", 1), ("belt", 2), ("filter", 1)))
    _sale(data, customer=customer, items=(("bolt", 3), ("belt", 1)))
    _sale(data, customer=customer, items=(("filter", 2), ("bolt", 1)))

    resp = _sales_detail(client, customer)
    assert resp.status_code == 200
    assert len(resp.context["page_obj"].object_list) == 7, (
        "на экране не строки деталей, а что-то другое"
    )


def test_sales_detail_does_not_show_document_numbers(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer)

    body = _sales_detail(client, customer).content.decode()
    assert sale.number not in body, "номер документа снова стал главным элементом"
    assert "Болт" in body


# --- B: дата слева ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url_name", "maker"),
    [("reports_sales_by_client_detail", "sale"), ("reports_repairs_by_client_detail", "repair")],
)
def test_date_is_the_first_column(client, data, admin, url_name, maker):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    if maker == "sale":
        _sale(data, customer=customer)
    else:
        _repair(data, customer=customer)

    body = client.get(reverse(url_name), {"customer_id": customer.pk}).content.decode()
    header = body[body.index("<thead>") + len("<thead>"):body.index("</thead>")]
    columns = [cell.strip() for cell in re.findall(r"<th[^>]*>(.*?)</th>", header, re.S)]
    assert columns, "у таблицы нет заголовка"
    assert columns[0] == "Дата", f"первая колонка не дата: {columns}"


# --- C: историческая сумма не переписывается текущим каталогом -----------------------------


def test_historical_amount_survives_a_catalog_price_change(client, data, admin):
    """Вчерашняя продажа стоила столько, за сколько её провели.

    Изменение рекомендованной цены детали меняет будущие продажи, но прошлую
    сумму трогать не имеет права.
    """
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("bolt", 2),), price=Decimal("496"))
    expected = Decimal("992")

    before = _sales_detail(client, customer).context["page_obj"].object_list
    assert before[0].total_price == expected

    part = data["parts"]["bolt"]
    part.recommended_price = Decimal("9999")
    part.save(update_fields=["recommended_price"])

    after = _sales_detail(client, customer).context["page_obj"].object_list
    assert after[0].total_price == expected, "историческая сумма изменилась вслед за каталогом"


# --- D, E, F, G: с клиентского экрана убран внутренний шум ---------------------------------


@pytest.mark.parametrize("noise", ["Себестоимость", "Прибыль", "Источник остатка", "лот #"])
def test_sales_detail_hides_internal_accounting(client, data, admin, noise):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    body = _sales_detail(client, customer).content.decode()
    assert noise not in body, f"на клиентском экране осталось «{noise}»"


@pytest.mark.parametrize("noise", ["Прибыль", "Источник остатка", "лот #"])
def test_repairs_detail_hides_internal_accounting(client, data, admin, noise):
    """На экране ремонтов остаётся только то, что нужно для работы.

    Себестоимость из этого списка убрана намеренно: сотруднику нужно знать, во
    сколько складу обошлась выданная деталь, и это прямое требование. Она
    показана отдельной колонкой, закрыта правом на закупочные суммы и никогда
    не называется выручкой. Прибыль, источник остатка и номера лотов
    по-прежнему лишние.
    """
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer)

    body = _repairs_detail(client, customer).content.decode()
    assert noise not in body, f"на клиентском экране осталось «{noise}»"


def test_repairs_detail_shows_the_issued_cost_but_never_calls_it_revenue(client, data, admin):
    """Стоимость выданной детали нужна, но выручкой ремонта она не является."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer)

    body = _repairs_detail(client, customer).content.decode()
    assert "Себестоимость (₽)" in body, "стоимость выданной детали не показана"
    assert "Выручка" not in body, "себестоимость выдана за выручку"


def test_the_data_itself_is_not_destroyed(client, data, admin):
    """Убрано с экрана, но не из базы: расследование по-прежнему возможно."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer)

    line = sale.lines.first()
    assert line.stock_lot_id is not None, "потеряна связь строки с источником остатка"
    assert line.total_cost_rub is not None, "потеряна себестоимость строки"


# --- H: период сохраняется ----------------------------------------------------------------


def test_period_is_preserved_in_the_client_screen(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    resp = client.get(
        reverse("reports_sales_by_client_detail"),
        {"customer_id": customer.pk, "date_from": "2026-07-21", "date_to": "2026-08-19"},
    )
    assert resp.status_code == 200
    period = resp.context["period"]
    assert str(period.date_from) == "2026-07-21"
    assert str(period.date_to) == "2026-08-19"
    assert "date_from=2026-07-21" in resp.context["period_qs"]
    assert "date_to=2026-08-19" in resp.context["period_qs"]


def test_the_back_link_carries_the_period(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    body = client.get(
        reverse("reports_sales_by_client_detail"),
        {"customer_id": customer.pk, "date_from": "2026-07-21", "date_to": "2026-08-19"},
    ).content.decode()
    back = reverse("reports_sales_by_client")
    assert f'{back}?date_from=2026-07-21&amp;date_to=2026-08-19' in body, (
        "возврат к списку клиентов теряет выбранный период"
    )


# --- I: одна деталь в разные дни остаётся разными строками ---------------------------------


def test_the_same_part_on_different_days_stays_separate_rows(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    first = _sale(data, customer=customer, items=(("bolt", 1),))
    second = _sale(data, customer=customer, items=(("bolt", 1),))
    third = _sale(data, customer=customer, items=(("bolt", 1),))
    for offset, sale in enumerate((first, second, third), start=1):
        Sale.objects.filter(pk=sale.pk).update(
            sold_at=timezone.now() - timezone.timedelta(days=offset)
        )

    rows = _sales_detail(client, customer).context["page_obj"].object_list
    assert len(rows) == 3, "покупки одной детали в разные дни склеены в одну строку"
    dates = [row.sale.sold_at for row in rows]
    assert dates == sorted(dates, reverse=True), "новые записи не сверху"


# --- J: количество внутри одного документа остаётся как в документе -----------------------


def test_quantity_within_one_sale_is_kept_as_the_document_has_it(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("bolt", 2),))

    rows = _sales_detail(client, customer).context["page_obj"].object_list
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("2")


# --- K: отменённые документы ---------------------------------------------------------------


def test_only_completed_sales_reach_the_client_screen(client, data, admin):
    """Политика не меняется: в отчёт попадают только проведённые продажи."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("bolt", 1),))
    draft = create_sale(customer=customer, customer_name="", by=data["admin"])
    add_stock_lot_to_sale(
        draft, data["lots"]["belt"], Decimal("5"), unit_price=UNIT_PRICE, by=data["admin"]
    )

    rows = _sales_detail(client, customer).context["page_obj"].object_list
    assert len(rows) == 1, "непроведённая продажа попала в историю клиента"
    assert rows[0].part_type_id == data["parts"]["bolt"].pk


# --- L: права ------------------------------------------------------------------------------


def test_a_user_without_report_rights_is_refused_by_direct_url(client, data, make_user):
    """Продавец отчётов не имеет по замыслу ролей, и прямая ссылка это уважает."""
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)
    seller = make_user("prodavec", role=roles.SELLER)
    _login(client, seller)

    resp = _sales_detail(client, customer)
    assert resp.status_code in (403, 302), "история клиента открылась без права на отчёты"


def test_the_menu_matches_the_permission(client, data, make_user):
    """Меню не должно предлагать то, что потом ответит отказом."""
    seller = make_user("prodavec2", role=roles.SELLER)
    _login(client, seller)

    body = client.get(reverse("dashboard")).content.decode()
    assert reverse("reports_dashboard") not in body, (
        "меню показывает отчёты роли, которой они запрещены"
    )


def test_reports_without_cost_right_still_show_customer_amount(client, data, make_user):
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)
    keeper = make_user("kladovshik", role=roles.STOREKEEPER)
    _login(client, keeper)

    resp = _sales_detail(client, customer)
    assert resp.status_code == 200
    assert resp.context["show_costs"] is False
    assert "Сумма (₽)" in resp.content.decode()


# --- M: без N+1 ----------------------------------------------------------------------------


def test_the_client_screen_does_not_grow_queries_with_rows(client, data, admin):
    """Число запросов не должно расти вместе с числом строк."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _login(client, admin)
    small = Customer.objects.create(name="Малый")
    _sale(data, customer=small, items=(("bolt", 1),))

    big = Customer.objects.create(name="Большой")
    for _ in range(12):
        _sale(data, customer=big, items=(("bolt", 1), ("belt", 1), ("filter", 1)))

    with CaptureQueriesContext(connection) as few:
        _sales_detail(client, small)
    with CaptureQueriesContext(connection) as many:
        resp = _sales_detail(client, big)

    assert len(resp.context["page_obj"].object_list) >= 20
    assert len(many) <= len(few) + 2, (
        f"запросы растут вместе со строками: {len(few)} против {len(many)}"
    )


# --- N: ремонты сразу показывают выданные детали -------------------------------------------


def test_repair_detail_shows_issued_part_rows_directly(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 2), ("filter", 1)))

    resp = _repairs_detail(client, customer)
    rows = resp.context["page_obj"].object_list
    assert len(rows) == 2, "на экране не выданные детали"
    body = resp.content.decode()
    assert order.number not in body, "ремонтный заказ снова стал главным элементом"
    assert "Ремень" in body and "Фильтр" in body


# --- O: себестоимость ремонта не выдаётся за деньги клиента --------------------------------


def test_repair_customer_amount_is_shown_separately_from_cost(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer, items=(("belt", 1),))

    body = _repairs_detail(client, customer).content.decode()
    assert "Сумма (₽)" in body

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    repair_rows = [row for row in rows if row["kind"] == "repair"]
    assert repair_rows, "проверка бессмысленна: строк ремонта нет"
    assert all(row["amount"] is not None for row in repair_rows)
    assert all(row["amount"] != row["cost"] for row in repair_rows)


# --- P, Q: идентичность клиента не тронута --------------------------------------------------


def test_card_documents_are_selected_by_the_key_not_by_the_name(data):
    """Переименование карточки не разрывает историю, однофамильцы не сливаются."""
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("bolt", 1),))
    customer.name = "Иванов Иван"
    customer.save(update_fields=["name"])
    _sale(data, customer=customer, items=(("belt", 1),))

    namesake = Customer.objects.create(name="Иванов Иван")
    _sale(data, customer=namesake, items=(("filter", 1),))

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert len(rows) == 2, "история карточки разорвана переименованием"
    assert {row["part_name"] for row in rows} == {"Болт", "Ремень"}


def test_legacy_documents_are_not_auto_linked_by_name(data):
    """Документ без карточки к карточке сам не приклеивается."""
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("bolt", 1),))
    _sale(data, name="Иванов", items=(("belt", 1),))

    linked = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert {row["part_name"] for row in linked} == {"Болт"}

    legacy = get_client_part_history(resolve_period({}), customer_name="Иванов", missing=False)
    assert {row["part_name"] for row in legacy} == {"Ремень"}


# --- Пустое состояние ----------------------------------------------------------------------


def test_an_empty_period_says_so_in_words(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)
    Sale.objects.all().update(sold_at=timezone.now() - timezone.timedelta(days=400))

    body = _sales_detail(client, customer).content.decode()
    assert "За выбранный период продаж нет" in body


def test_an_empty_repair_period_says_so_in_words(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")

    body = _repairs_detail(client, customer).content.decode()
    assert "За выбранный период ремонтов нет" in body


def test_a_representative_client_stays_within_a_query_budget(client, data, admin):
    """Пятьдесят документов и двести строк: запросы не должны расти со строками.

    Масштаб взят как у активного клиента за период. Проверяется и число
    запросов, и то, что объединённый экран клиента остаётся в том же бюджете,
    что и продажи по отдельности.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _login(client, admin)
    quiet = Customer.objects.create(name="Тихий")
    _sale(data, customer=quiet, items=(("bolt", 1),))

    busy = Customer.objects.create(name="Активный")
    for _ in range(50):
        _sale(data, customer=busy, items=(("bolt", 1), ("belt", 1), ("filter", 1), ("gasket", 1)))

    with CaptureQueriesContext(connection) as baseline:
        _sales_detail(client, quiet)
    with CaptureQueriesContext(connection) as loaded:
        resp = _sales_detail(client, busy)
    assert len(loaded) <= len(baseline) + 2, (
        f"продажи клиента: {len(baseline)} против {len(loaded)} запросов"
    )

    total = resp.context["page_obj"].paginator.count
    assert total >= 150, f"проверка бессмысленна: строк всего {total}"

    with CaptureQueriesContext(connection) as combined:
        client.get(reverse("reports_client_timeline"), {"customer_id": busy.pk})
    assert len(combined) <= len(baseline) + 4, (
        f"объединённый экран клиента вышел из бюджета: {len(combined)} запросов"
    )
