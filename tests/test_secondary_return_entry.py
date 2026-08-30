"""Возврат с выбором ячейки и состояния как вторичное действие документа.

Ссылка «Оформить возврат» ушла из шапок продажи и ремонта, и вместе с ней
пропал единственный вход в возврат, где выбираются ячейка и состояние - в том
числе карантин. Обычная отмена так не умеет: она всегда возвращает деталь в её
же ячейку доступной.

Здесь закреплено: главное действие документа осталось одно, а вход в возврат
вернулся отдельным разделом ниже, не споря с ним за внимание. Ссылка ведёт в
тот же канонический маршрут с теми же правами и показывается только тогда,
когда возврат действительно возможен.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.models import StockReturnLine
from apps.returns.services import (
    add_repair_line_return,
    add_sale_line_return,
    complete_return,
    create_return,
)
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
# Подписи различаются потому, что различаются сами возможности: у продажи
# ячейка возврата выбирается, у ремонта она всегда исходная и выбирается только
# состояние. Обещать операторам одинаковое было бы неправдой.
SALE_LABEL = "Возврат в другую ячейку / карантин"
REPAIR_LABEL = "Возврат в карантин"


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


@pytest.fixture
def data(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="Ремень",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("900"),
    )
    PartNumber.objects.create(part=part, value="700200", kind=PartNumber.Kind.OEM)
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
    return {"admin": admin, "location": location, "part": part, "lot": lot}


def _sale(data, quantity=2, *, complete=True):
    sale = create_sale(customer_name="Иванов", by=data["admin"])
    add_stock_lot_to_sale(
        sale, data["lot"], Decimal(str(quantity)), unit_price=Decimal("500"), by=data["admin"]
    )
    if not complete:
        return sale
    return complete_sale(sale, by=data["admin"])


def _repair(data, quantity=2, *, complete=True):
    order = create_repair_order(customer_name="Иванов", by=data["admin"])
    add_stock_lot_to_repair_order(
        order,
        data["lot"],
        Decimal(str(quantity)),
        customer_unit_price_rub=Decimal("900"),
        by=data["admin"],
    )
    if not complete:
        return order
    return complete_repair_order(order, by=data["admin"])


def _login(client, user):
    client.login(username=user.username, password=PASSWORD)


def _sale_page(client, sale):
    return client.get(reverse("sale_detail", args=[sale.pk])).content.decode()


def _repair_page(client, order):
    return client.get(reverse("repair_order_detail", args=[order.pk])).content.decode()


def _header(body):
    """Верхняя строка действий документа - всё до первой таблицы."""
    return body.split("<table", 1)[0]


# --- Продажа -----------------------------------------------------------------


def test_the_sale_keeps_exactly_one_main_action(client, data, admin):
    _login(client, admin)
    body = _sale_page(client, _sale(data))

    header = _header(body)
    assert "Отменить продажу" in header
    assert "Оформить возврат" not in body, "конкурирующая ссылка вернулась"
    assert "return_create" not in header
    assert reverse("return_create") not in header, "возврат снова спорит с главным действием"


def test_the_sale_offers_the_special_return_below(client, data, admin):
    _login(client, admin)
    sale = _sale(data)

    body = _sale_page(client, sale)

    assert "<h2>Возвраты</h2>" in body
    expected = f'{reverse("return_create")}?source=sale&amp;id={sale.pk}'
    assert expected in body, "ссылка не ведёт в канонический маршрут возврата"
    assert SALE_LABEL in body
    assert body.index("Отменить продажу") < body.index(SALE_LABEL), "вторичное встало выше главного"


def test_the_sale_link_is_hidden_without_the_return_right(client, data, make_user):
    """Продавцу возврат не выдан намеренно: ссылку он видеть не должен."""
    seller = make_user("prodavec", role=roles.SELLER)
    sale = _sale(data)

    _login(client, seller)
    body = _sale_page(client, sale)

    assert SALE_LABEL not in body
    assert reverse("return_create") not in body


def test_a_draft_sale_offers_no_return(client, data, admin):
    _login(client, admin)
    draft = _sale(data, complete=False)

    body = _sale_page(client, draft)

    assert SALE_LABEL not in body


def test_a_fully_returned_sale_offers_no_dead_link(client, data, admin):
    """Возвращать больше нечего - ссылка вела бы в отказ, поэтому её нет."""
    sale = _sale(data, quantity=2)
    line = sale.lines.get()
    document = create_return(source=sale, by=admin)
    add_sale_line_return(
        document,
        line,
        Decimal("2"),
        to_location=data["location"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=admin,
    )
    complete_return(document, by=admin)

    _login(client, admin)
    body = _sale_page(client, sale)

    assert SALE_LABEL not in body
    assert "Отменить продажу" in _header(body), "главное действие пропало вместе со вторичным"


# --- Ремонт ------------------------------------------------------------------


def test_the_repair_keeps_exactly_one_main_action(client, data, admin):
    _login(client, admin)
    body = _repair_page(client, _repair(data))

    header = _header(body)
    assert "Отменить ремонт" in header
    assert "Оформить возврат" not in body
    assert reverse("return_create") not in header


def test_the_repair_offers_the_special_return_below(client, data, admin):
    _login(client, admin)
    order = _repair(data)

    body = _repair_page(client, order)

    assert "<h2>Возвраты</h2>" in body
    expected = f'{reverse("return_create")}?source=repair&amp;id={order.pk}'
    assert expected in body, "ремонт не ведёт в канонический маршрут возврата"
    assert REPAIR_LABEL in body
    assert body.index("Отменить ремонт") < body.index(REPAIR_LABEL)


def test_the_repair_link_is_hidden_without_the_return_right(client, data, make_user):
    storekeeper = make_user("master", role=roles.SELLER)
    order = _repair(data)

    _login(client, storekeeper)
    response = client.get(reverse("repair_order_detail", args=[order.pk]))

    if response.status_code == 200:
        assert REPAIR_LABEL not in response.content.decode()


def test_a_draft_repair_offers_no_return(client, data, admin):
    _login(client, admin)
    draft = _repair(data, complete=False)

    body = _repair_page(client, draft)

    assert REPAIR_LABEL not in body


def test_a_fully_returned_repair_offers_no_dead_link(client, data, admin):
    order = _repair(data, quantity=2)
    line = order.lines.get()
    document = create_return(source=order, by=admin)
    add_repair_line_return(
        document,
        line,
        Decimal("2"),
        to_location=data["location"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=admin,
    )
    complete_return(document, by=admin)

    _login(client, admin)
    body = _repair_page(client, order)

    assert REPAIR_LABEL not in body
    assert "Отменить ремонт" in _header(body)


# --- Маршрут и права остаются каноническими ----------------------------------


def test_the_link_opens_the_canonical_return_draft_screen(client, data, admin):
    _login(client, admin)
    sale = _sale(data)

    response = client.get(reverse("return_create"), {"source": "sale", "id": sale.pk})

    assert response.status_code == 200
    body = response.content.decode()
    assert "Оформить возврат на склад" in body


def test_the_repair_link_opens_the_same_canonical_screen(client, data, admin):
    _login(client, admin)
    order = _repair(data)

    response = client.get(reverse("return_create"), {"source": "repair", "id": order.pk})

    assert response.status_code == 200
    assert "Оформить возврат на склад" in response.content.decode()


def test_the_route_still_refuses_a_user_without_the_return_right(client, data, make_user):
    seller = make_user("prodavec", role=roles.SELLER)
    sale = _sale(data)

    _login(client, seller)
    response = client.get(reverse("return_create"), {"source": "sale", "id": sale.pk})

    assert response.status_code == 403


def test_the_sale_return_offers_a_choice_of_cell(client, data, admin):
    """Подпись у продажи обещает выбор ячейки - проверяем, что он есть."""
    _login(client, admin)
    sale = _sale(data)
    document = create_return(source=sale, by=admin)

    response = client.get(reverse("return_detail", args=[document.pk]))

    rows = response.context["source_rows"]
    assert rows, "строк к возврату нет"
    assert all(row["source_location"] is None for row in rows), (
        "ячейка у продажи предопределена, значит подпись обещает лишнее"
    )
    assert response.context["locations"], "выбирать ячейку не из чего"
    assert [choice[0] for choice in response.context["restock_choices"]] == [
        "available",
        "quarantine",
    ]


def test_the_repair_return_keeps_the_source_cell(client, data, admin):
    """Подпись у ремонта не обещает ячейку - потому что её выбора нет."""
    _login(client, admin)
    order = _repair(data)
    document = create_return(source=order, by=admin)

    response = client.get(reverse("return_detail", args=[document.pk]))

    rows = response.context["source_rows"]
    assert rows, "строк к возврату нет"
    assert all(row["source_location"] == data["location"] for row in rows), (
        "ячейка возврата из ремонта перестала быть исходной"
    )
    assert "quarantine" in [choice[0] for choice in response.context["restock_choices"]]
