"""История клиента как рабочий экран оператора.

Раньше строка клиента в отчёте несла две ссылки в одну и ту же карточку, а
сама история показывала складскую себестоимость вместо цены, по которой деталь
ушла клиенту. Отменить ошибочно проданную единицу можно было только через
документ продажи.

Здесь закреплено новое поведение: одна ссылка на карточку, отдельная «История»
и отдельный статус карточки, цена вместо себестоимости, действующее количество
и отмена ровно той строки прямо из истории. Снимки документов при этом
остаются неизменными: отменённое считается каноническими возвратами.
"""

import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairIssueLine
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import get_client_part_history, period_range, resolve_period
from apps.returns.models import StockReturn, StockReturnLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.models import SaleLine
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
SALE_PRICE = Decimal("554")


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


def _lot(part, location, qty, supplier, admin, *, unit_cost="100"):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(str(qty)),
        unit_cost_currency=Decimal(unit_cost),
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
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    parts, lots = {}, {}
    for key, name, number in (
        ("bolt", "Болт", "700100"),
        ("belt", "Ремень", "700200"),
    ):
        part = PartType.objects.create(
            name=name,
            category=category,
            unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal("900"),
        )
        PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM)
        parts[key] = part
        lots[key] = _lot(part, location, 400, supplier, admin)
    return {
        "supplier": supplier,
        "location": location,
        "admin": admin,
        "parts": parts,
        "lots": lots,
    }


def _sale(data, *, customer=None, name="", items=(("bolt", 1),), price=SALE_PRICE):
    sale = create_sale(customer=customer, customer_name=name, by=data["admin"])
    for key, quantity in items:
        add_stock_lot_to_sale(
            sale, data["lots"][key], Decimal(str(quantity)), unit_price=price, by=data["admin"]
        )
    return complete_sale(sale, by=data["admin"])


def _repair(data, *, customer=None, name="", items=(("belt", 1),), price=None):
    order = create_repair_order(customer=customer, customer_name=name, by=data["admin"])
    for key, quantity in items:
        add_stock_lot_to_repair_order(
            order,
            data["lots"][key],
            Decimal(str(quantity)),
            customer_unit_price_rub=price,
            by=data["admin"],
        )
    return complete_repair_order(order, by=data["admin"])


def _login(client, user):
    client.login(username=user.username, password=PASSWORD)


def _overview(client, **params):
    return client.get(reverse("reports_clients_overview"), params)


def _history(client, customer, **params):
    return client.get(
        reverse("reports_client_timeline"), {"customer_id": customer.pk, **params}
    )


def _history_url(customer, **params):
    from urllib.parse import urlencode

    query = urlencode({"customer_id": customer.pk, **params})
    return f"{reverse('reports_client_timeline')}?{query}"


# --- A: строка клиента в отчёте --------------------------------------------------------------


def test_the_row_does_not_carry_two_links_to_the_same_card(client, data, admin):
    """Имя клиента само ведёт в карточку, поэтому второй такой ссылки нет."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    body = _overview(client).content.decode()

    assert "Открыть карточку" not in body, "дублирующая ссылка на карточку осталась"
    card_url = reverse("customer_detail", args=[customer.pk])
    assert body.count(f'href="{card_url}"') == 1, "в строку вернулась вторая ссылка на карточку"


def test_the_history_link_is_written_as_a_name(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    body = _overview(client).content.decode()

    assert ">История</a>" in body
    assert ">история</a>" not in body


def test_the_card_status_reads_as_a_status_not_as_a_title(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    body = _overview(client).content.decode()

    assert "карточка есть" in body
    assert "Карточка есть" not in body


def test_history_and_card_status_are_separate_elements(client, data, admin):
    """Они не должны склеиваться в одну фразу «История карточка есть»."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    body = _overview(client).content.decode()

    between = body.split(">История</a>", 1)[1].split("карточка есть", 1)[0]
    assert "<span" in between, "статус не вынесен в отдельный элемент"
    assert "·" not in between, "статус снова приклеен к ссылке точкой"
    assert "row-meta" in body, "строка-подпись потеряла собственный контейнер"


def test_a_client_without_a_card_still_gets_one_created(client, data, admin):
    _login(client, admin)
    _sale(data, name="Пётр Безкарточный")

    body = _overview(client).content.decode()

    assert "Без карточки" in body
    assert "Создать карточку" in body
    assert reverse("legacy_customer_link") in body


# --- B: быстрые периоды ----------------------------------------------------------------------


def test_the_quick_periods_are_the_four_the_operator_works_with(client, data, admin):
    _login(client, admin)
    body = _overview(client).content.decode()

    for label in ("Сегодня", "7 дней", "Месяц", "За всё время"):
        assert f">{label}</a>" in body, f"кнопка «{label}» пропала"
    assert "30 дней" not in body, "«30 дней» вернулись в быстрые периоды"


def test_all_time_has_no_invented_start_date():
    """У «всего времени» границ нет вовсе, а не дата вроде 01.01.2020."""
    period = resolve_period({"preset": "all"})

    assert period.all_time is True
    assert period.date_from is None
    assert period.date_to is None
    assert period_range("sold_at", period) == {}
    assert "sold_at__range" in period_range("sold_at", resolve_period({"preset": "month"}))


def test_all_time_shows_documents_older_than_any_preset(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    old_sale = _sale(data, customer=customer)
    old_sale.sold_at = timezone.now() - timezone.timedelta(days=2000)
    old_sale.save(update_fields=["sold_at"])

    month = get_client_part_history(resolve_period({"preset": "month"}), customer_id=customer.pk)
    assert month == []
    rows = get_client_part_history(resolve_period({"preset": "all"}), customer_id=customer.pk)

    assert len(rows) == 1
    assert rows[0]["quantity"] == Decimal("1")


def test_named_dates_win_over_a_preset_left_in_the_address():
    """Оператор выбрал числа - он получает именно их, а не прежний пресет."""
    period = resolve_period(
        {"preset": "all", "date_from": "2026-01-01", "date_to": "2026-01-31"}
    )

    assert period.all_time is False
    assert period.preset == ""
    assert period.date_from.isoformat() == "2026-01-01"


def test_all_time_survives_the_way_into_the_history_and_back(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    body = _overview(client, preset="all").content.decode()
    assert "preset=all" in body, "ссылка в историю потеряла режим «за всё время»"

    history = _history(client, customer, preset="all")
    assert history.context["period"].all_time is True
    assert "Период: за всё время." in history.content.decode()


def test_payment_cannot_be_acknowledged_without_a_period(client, data, admin):
    """Отметка оплаты живёт в границах периода: у «всего времени» их нет."""
    from apps.customers.models import CustomerPeriodPaymentAcknowledgement

    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)

    body = _overview(client, preset="all").content.decode()
    assert "Выберите период" in body

    response = client.post(
        reverse("reports_client_period_payment_status"),
        {"customer_id": customer.pk, "paid": "1", "preset": "all"},
    )

    assert response.status_code == 302
    assert not CustomerPeriodPaymentAcknowledgement.objects.exists()


# --- C: таблица истории ----------------------------------------------------------------------


def test_the_history_does_not_show_warehouse_cost(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer)

    response = _history(client, customer)

    assert response.context["show_costs"] is True, "право на закупочные суммы у админа есть"
    assert "Себестоимость" not in response.content.decode()


def test_the_warehouse_cost_stays_in_the_data(data, admin):
    """Убрана колонка, а не сама величина: складским отчётам она нужна."""
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer, items=(("belt", 2),))

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)

    assert rows[0]["cost"] == Decimal("200.00")


def test_the_history_shows_the_price_the_client_paid(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer, items=(("bolt", 2),))

    response = _history(client, customer)
    body = response.content.decode()

    assert "Цена (₽)" in body
    row = response.context["page_obj"].object_list[0]
    assert row["unit_price"] == SALE_PRICE
    assert row["amount"] == Decimal("1108.00")


def test_the_price_is_historical_and_ignores_the_catalog(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _sale(data, customer=customer)
    PartType.objects.filter(pk=data["parts"]["bolt"].pk).update(recommended_price=Decimal("9999"))

    row = _history(client, customer).context["page_obj"].object_list[0]

    assert row["unit_price"] == SALE_PRICE
    assert row["price_source"] == "historical"


def test_a_repair_uses_its_own_frozen_customer_price(data, admin):
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer, items=(("belt", 2),), price=Decimal("777"))

    row = get_client_part_history(resolve_period({}), customer_id=customer.pk)[0]

    assert row["unit_price"] == Decimal("777")
    assert row["price_source"] == "historical"
    assert row["amount"] == Decimal("1554.00")


def test_an_old_repair_price_is_marked_as_the_current_one(client, data, admin):
    """Текущая цена не выдаётся за исторический снимок: она подписана."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    _repair(data, customer=customer)
    RepairIssueLine.objects.update(customer_unit_price_rub=None)

    response = _history(client, customer)
    row = response.context["page_obj"].object_list[0]

    assert row["price_source"] == "current_fallback"
    assert row["unit_price"] == Decimal("900")
    assert "текущая цена" in response.content.decode()


def test_the_sum_follows_the_effective_quantity(client, data, admin):
    """Продано 4 по 554, отменена 1: остаётся 3 по 554 на 1662."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 4),))
    line = sale.lines.get()

    from apps.sales.services import cancel_sale_line_quantity

    cancel_sale_line_quantity(line, 1, reason="Ошибка", author="И.", by=admin)

    row = _history(client, customer).context["page_obj"].object_list[0]

    assert row["quantity"] == Decimal("3")
    assert row["unit_price"] == SALE_PRICE
    assert row["amount"] == Decimal("1662.00")
    assert row["issued_quantity"] == Decimal("4")
    assert row["reversed_quantity"] == Decimal("1")


def test_the_document_snapshot_is_never_rewritten(data, admin):
    from apps.sales.services import cancel_sale_line_quantity

    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 4),))
    line = sale.lines.get()

    cancel_sale_line_quantity(line, 1, reason="Ошибка", author="И.", by=admin)

    line.refresh_from_db()
    assert line.quantity == Decimal("4"), "переписано количество проданного"
    assert line.unit_price == SALE_PRICE, "переписана цена продажи"


# --- D: отмена ровно этой строки прямо из истории --------------------------------------------


def test_every_eligible_row_offers_cancelling_exactly_its_own_line(client, data, admin):
    """Одна и та же деталь в двух продажах даёт две разные ссылки отмены."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    first = _sale(data, customer=customer, items=(("bolt", 1),))
    second = _sale(data, customer=customer, items=(("bolt", 1),))

    body = _history(client, customer).content.decode()

    for sale in (first, second):
        url = reverse("sale_line_cancel", args=[sale.lines.get().pk])
        assert f'href="{url}?next=' in body, "строка не знает своей точной строки продажи"


def test_a_repair_row_offers_cancelling_its_own_issue_line(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    order = _repair(data, customer=customer, items=(("belt", 2),))

    body = _history(client, customer).content.decode()

    url = reverse("repair_line_cancel", args=[order.lines.get().pk])
    assert f'href="{url}?next=' in body


def test_cancelling_needs_no_document_and_returns_to_the_history(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 4),))
    line = sale.lines.get()
    back = _history_url(customer, preset="all")

    confirm = client.get(reverse("sale_line_cancel", args=[line.pk]), {"next": back})
    assert confirm.status_code == 200
    assert "700100" in confirm.content.decode(), "в подтверждении нет артикула"

    response = client.post(
        reverse("sale_line_cancel", args=[line.pk]),
        {"quantity": "1", "reason": "Ошибка", "author": "Иван", "next": back},
    )

    assert response.status_code == 302
    assert response["Location"] == back, "оператора увели с его же страницы истории"
    row = _history(client, customer, preset="all").context["page_obj"].object_list[0]
    assert row["quantity"] == Decimal("3")


def test_the_quantity_offered_is_bounded_by_what_is_left(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 4),))
    line = sale.lines.get()

    response = client.get(reverse("sale_line_cancel", args=[line.pk]))

    assert response.context["remaining"] == Decimal("4")
    assert response.context["form"].fields["quantity"].widget.attrs["max"] == "4"


def test_an_earlier_ordinary_return_lowers_the_upper_bound(client, data, admin):
    """Одну единицу нельзя вернуть дважды: обычный возврат тоже учитывается."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 4),))
    line = sale.lines.get()
    document = create_return(source=sale, by=admin)
    add_sale_line_return(
        document,
        line,
        Decimal("1"),
        to_location=data["location"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=admin,
    )
    complete_return(document, by=admin)

    response = client.get(reverse("sale_line_cancel", args=[line.pk]))
    assert response.context["remaining"] == Decimal("3")

    refused = client.post(
        reverse("sale_line_cancel", args=[line.pk]),
        {"quantity": "4", "reason": "Слишком много", "author": "И."},
    )
    assert refused.status_code == 200
    line.refresh_from_db()
    assert line.quantity == Decimal("4")


def test_a_fully_reversed_row_offers_no_action(client, data, admin):
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 2),))
    line = sale.lines.get()

    from apps.sales.services import cancel_sale_line_quantity

    cancel_sale_line_quantity(line, 2, reason="Всё", author="И.", by=admin)

    body = _history(client, customer).content.decode()

    assert reverse("sale_line_cancel", args=[line.pk]) not in body
    assert "Отменено" in body


def test_the_cancelled_unit_goes_back_to_its_own_lot_and_cell(data, admin):
    from apps.sales.services import cancel_sale_line_quantity

    customer = Customer.objects.create(name="Иванов")
    lot = data["lots"]["bolt"]
    sale = _sale(data, customer=customer, items=(("bolt", 3),))
    lot.refresh_from_db()
    after_sale = lot.quantity

    cancel_sale_line_quantity(sale.lines.get(), 1, reason="Ошибка", author="И.", by=admin)

    lot.refresh_from_db()
    assert lot.quantity == after_sale + Decimal("1")
    movement = StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_LOT
    ).latest("pk")
    assert movement.stock_lot_id == lot.pk
    assert movement.to_location_id == data["location"].pk


def test_the_payment_acknowledgement_goes_stale_after_a_cancellation(client, data, admin):
    """Частичная отмена меняет сумму к оплате, поэтому подтверждение слетает."""
    from apps.reports.payment_status import payment_statuses_for_rows
    from apps.sales.services import cancel_sale_line_quantity

    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 4),))
    period = resolve_period({})
    rows = [{"customer_id": customer.pk, "linked": True}]

    _login(client, admin)
    client.post(
        reverse("reports_client_period_payment_status"),
        {
            "customer_id": customer.pk,
            "paid": "1",
            "date_from": period.date_from.isoformat(),
            "date_to": period.date_to.isoformat(),
        },
    )
    assert payment_statuses_for_rows(rows=rows, period=period)[customer.pk]["paid"] is True

    cancel_sale_line_quantity(sale.lines.get(), 1, reason="Ошибка", author="И.", by=admin)

    assert payment_statuses_for_rows(rows=rows, period=period)[customer.pk]["paid"] is False


# --- Права ------------------------------------------------------------------------------------


def test_without_the_return_right_the_button_is_absent_and_the_post_is_refused(
    client, data, make_user
):
    """Продавцу возврат не выдан намеренно: отмена не должна стать обходом."""
    seller = make_user("prodavec", role=roles.SELLER)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 2),))
    line = sale.lines.get()

    _login(client, seller)
    refused = client.post(
        reverse("sale_line_cancel", args=[line.pk]),
        {"quantity": "1", "reason": "Тайком", "author": "П."},
    )

    assert refused.status_code == 403
    line.refresh_from_db()
    assert line.quantity == Decimal("2")
    assert not StockReturn.objects.exists()


def test_a_manager_without_returns_sees_no_cancel_action(client, data, make_user, admin):
    manager = make_user("upravlyayushchiy", role=roles.MANAGER)
    manager.groups.clear()
    manager.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 2),))

    _login(client, manager)
    response = _history(client, customer)

    if response.status_code == 200:
        assert reverse("sale_line_cancel", args=[sale.lines.get().pk]) not in (
            response.content.decode()
        )


def test_the_cancel_link_never_leaves_the_site(client, data, admin):
    """Чужой адрес в next не должен превращать отмену в переходник наружу."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer, items=(("bolt", 2),))

    response = client.post(
        reverse("sale_line_cancel", args=[sale.lines.get().pk]),
        {
            "quantity": "1",
            "reason": "Ошибка",
            "author": "И.",
            "next": "https://example.org/phish",
        },
    )

    assert response.status_code == 302
    assert "example.org" not in response["Location"]


# --- Запросы ------------------------------------------------------------------------------------


def test_the_history_does_not_grow_queries_with_rows(client, data, admin):
    from django.test.utils import CaptureQueriesContext

    _login(client, admin)
    quiet = Customer.objects.create(name="Тихий")
    _sale(data, customer=quiet, items=(("bolt", 1),))
    _repair(data, customer=quiet, items=(("belt", 1),))
    busy = Customer.objects.create(name="Активный")
    for _ in range(12):
        _sale(data, customer=busy, items=(("bolt", 1), ("belt", 1)))
        _repair(data, customer=busy, items=(("belt", 1),))

    with CaptureQueriesContext(connection) as few:
        _history(client, quiet)
    with CaptureQueriesContext(connection) as many:
        response = _history(client, busy)

    assert len(response.context["page_obj"].object_list) >= 30
    assert len(many) <= len(few) + 2, (
        f"запросы растут вместе со строками: {len(few)} против {len(many)}"
    )


# --- Параллельная отмена через сам экран истории -------------------------------------------


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_postgresql_two_history_cancellations_of_the_last_unit(django_user_model, client):
    """Две отмены последней единицы через экран истории: пройти должна одна."""
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections
    from django.test import Client

    if connection.vendor != "postgresql":
        pytest.skip("Нужен PostgreSQL через DENSTOCK_TEST_DATABASE_URL")

    admin = django_user_model.objects.create_superuser(username="parallel", password=PASSWORD)
    supplier = Supplier.objects.create(name="ООО Поставка")
    location = StorageLocation.objects.create(
        name="Ячейка", code="PH-01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="ГАЙКА",
        category=Category.objects.create(name="Параллель"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("100"),
    )
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    batch_line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("5"), unit_cost_currency=Decimal("100")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    batch_line.refresh_from_db()
    lot = create_stock_lot(batch_line, location, Decimal("5"))
    receive_stock_lot(lot, by=admin)

    customer = Customer.objects.create(name="Гонка")
    sale = create_sale(customer=customer, customer_name="", by=admin)
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("100"), by=admin)
    complete_sale(sale, by=admin)
    line = sale.lines.get()
    url = reverse("sale_line_cancel", args=[line.pk])
    back = _history_url(customer)

    def attempt():
        close_old_connections()
        try:
            session = Client()
            session.login(username="parallel", password=PASSWORD)
            response = session.post(
                url,
                {"quantity": "1", "reason": "Гонка", "author": "И.", "next": back},
                follow=True,
            )
            body = response.content.decode()
            return "ok" if "Отменено 1 из позиции" in body else "refused"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=60) for f in (pool.submit(attempt), pool.submit(attempt))]

    lot.refresh_from_db()
    assert lot.quantity == Decimal("5"), "вернулось больше, чем было продано"
    assert results.count("ok") == 1, f"обе отмены прошли: {results}"
    assert (
        StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.RETURN_LOT, stock_lot=lot
        ).count()
        == 1
    )
    assert SaleLine.objects.get(pk=line.pk).quantity == Decimal("1")


def test_the_history_row_carries_no_document_number(client, data, admin):
    """Номер документа тут лишний: вопрос про деталь, а не про бумагу."""
    _login(client, admin)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale(data, customer=customer)

    body = _history(client, customer).content.decode()

    assert not re.search(rf">\s*{re.escape(sale.number)}\s*<", body)
