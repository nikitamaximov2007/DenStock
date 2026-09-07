"""Stage 7 — до подтверждения отмены видно, куда физически вернётся товар.

Сотрудник подтверждал отмену вслепую: экран обещал «склад будет компенсирован
по исходным строкам», а в какую ячейку идти с деталью, выяснялось уже после.
При нескольких лотах и нескольких ячейках это означало поиск по складу.

Ячейку задаёт СТРОКА ДОКУМЕНТА, а не текущая ячейка карточки детали: деталь
могла переехать после продажи, и вернуть её нужно туда, откуда её взял этот
документ. Предпросмотр и сама отмена считают это одним и тем же кодом, поэтому
разойтись они не могут.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import StockBalance, StockLot
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
    repair_cancellation_returns,
)
from apps.returns.services import ReturnAllocation
from apps.sales.models import Sale
from apps.sales.services import (
    add_stock_lot_to_sale,
    cancel_sale,
    complete_sale,
    create_sale,
    sale_cancellation_returns,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

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


def _lot(part, location, qty, sup, admin, *, unit_cost="100"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(str(qty)),
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
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    cell_a = StorageLocation.objects.create(
        name="Ячейка A", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    cell_b = StorageLocation.objects.create(
        name="Ячейка B", code="S02-D03-C01", storage_allowed=True, is_active=True
    )
    bolt = PartType.objects.create(
        name="Болт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=bolt, value="700100", kind=PartNumber.Kind.OEM)
    return {
        "sup": sup, "admin": admin, "cell_a": cell_a, "cell_b": cell_b, "bolt": bolt,
        "lot_a": _lot(bolt, cell_a, 2, sup, admin),
        "lot_b": _lot(bolt, cell_b, 5, sup, admin, unit_cost="200"),
    }


def _sale_from(data, portions):
    """Проведённая продажа, собранная из конкретных лотов."""
    sale = create_sale(customer_name="Иванов", by=data["admin"])
    for lot, qty in portions:
        add_stock_lot_to_sale(sale, lot, Decimal(str(qty)), unit_price=Decimal("500"),
                              by=data["admin"])
    return complete_sale(sale, by=data["admin"])


def _repair_from(data, portions):
    order = create_repair_order(customer_name="Иванов", by=data["admin"])
    for lot, qty in portions:
        add_stock_lot_to_repair_order(order, lot, Decimal(str(qty)),
                                      customer_unit_price_rub=Decimal("500"), by=data["admin"])
    return complete_repair_order(order, by=data["admin"])


def _by_cell(allocations):
    totals = {}
    for allocation in allocations:
        code = allocation.location.code
        totals[code] = totals.get(code, Decimal("0")) + allocation.quantity
    return totals


def _login(client, make_user, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


# --- Предпросмотр ------------------------------------------------------------------------


def test_preview_names_the_cell_of_the_document_line(data):
    sale = _sale_from(data, [(data["lot_b"], 3)])
    allocations = sale_cancellation_returns(sale)
    assert _by_cell(allocations) == {"S02-D03-C01": Decimal("3")}


def test_preview_splits_a_multi_cell_sale_across_all_its_cells(data):
    sale = _sale_from(data, [(data["lot_a"], 2), (data["lot_b"], 1)])
    assert _by_cell(sale_cancellation_returns(sale)) == {
        "S01-D01-C01": Decimal("2"),
        "S02-D03-C01": Decimal("1"),
    }


def test_preview_returns_allocation_objects(data):
    sale = _sale_from(data, [(data["lot_a"], 1)])
    allocation = sale_cancellation_returns(sale)[0]
    assert isinstance(allocation, ReturnAllocation)
    assert allocation.part == data["bolt"]
    assert allocation.quantity == Decimal("1")


def test_preview_does_not_follow_the_part_to_a_new_cell(data):
    """Деталь переехала после продажи: возврат всё равно идёт в ячейку строки."""
    sale = _sale_from(data, [(data["lot_a"], 2)])
    _lot(data["bolt"], data["cell_b"], 4, data["sup"], data["admin"])
    assert _by_cell(sale_cancellation_returns(sale)) == {"S01-D01-C01": Decimal("2")}


def test_repair_preview_splits_across_cells_too(data):
    order = _repair_from(data, [(data["lot_a"], 2), (data["lot_b"], 2)])
    assert _by_cell(repair_cancellation_returns(order)) == {
        "S01-D01-C01": Decimal("2"),
        "S02-D03-C01": Decimal("2"),
    }


def test_a_draft_has_nothing_to_return(data):
    sale = create_sale(customer_name="Иванов", by=data["admin"])
    assert sale_cancellation_returns(sale) == []


# --- Предпросмотр совпадает с фактом ------------------------------------------------------


def _balances(part):
    """Физический остаток детали по ячейкам. Строк на ячейку может быть много."""
    totals = {}
    for balance in StockBalance.objects.filter(part_type=part).select_related("location"):
        code = balance.location.code
        totals[code] = totals.get(code, Decimal("0")) + balance.quantity_physical
    return totals


def test_cancellation_returns_exactly_what_the_preview_promised(data):
    sale = _sale_from(data, [(data["lot_a"], 2), (data["lot_b"], 1)])
    promised = _by_cell(sale_cancellation_returns(sale))
    before = _balances(data["bolt"])
    cancel_sale(sale, by=data["admin"], reason="Ошибка", author="Иванов")
    after = _balances(data["bolt"])
    actual = {
        code: after.get(code, Decimal("0")) - before.get(code, Decimal("0"))
        for code in set(before) | set(after)
        if after.get(code, Decimal("0")) != before.get(code, Decimal("0"))
    }
    assert actual == promised


def test_repair_cancellation_matches_its_preview(data):
    order = _repair_from(data, [(data["lot_a"], 1), (data["lot_b"], 3)])
    promised = _by_cell(repair_cancellation_returns(order))
    before = _balances(data["bolt"])
    cancel_repair_order(order, by=data["admin"], reason="Ошибка", author="Иванов")
    after = _balances(data["bolt"])
    actual = {
        code: after.get(code, Decimal("0")) - before.get(code, Decimal("0"))
        for code in set(before) | set(after)
        if after.get(code, Decimal("0")) != before.get(code, Decimal("0"))
    }
    assert actual == promised


def test_preview_after_a_partial_return_shows_only_the_rest(data):
    """Часть уже вернули оформленным возвратом: отмене остаётся остаток."""
    from apps.returns.services import add_sale_line_return, complete_return, create_return

    sale = _sale_from(data, [(data["lot_b"], 4)])
    line = sale.lines.get()
    ret = create_return(source=sale, reason="Часть вернули", by=data["admin"])
    add_sale_line_return(
        ret, line, Decimal("3"), to_location=data["cell_b"],
        restock_status=StockLot.Status.AVAILABLE, by=data["admin"],
    )
    complete_return(ret, by=data["admin"])

    promised = _by_cell(sale_cancellation_returns(sale))
    assert promised == {"S02-D03-C01": Decimal("1")}

    before = _balances(data["bolt"])
    cancel_sale(sale, by=data["admin"], reason="Ошибка", author="Иванов")
    after = _balances(data["bolt"])
    assert after["S02-D03-C01"] - before["S02-D03-C01"] == Decimal("1")


def test_a_fully_returned_sale_previews_nothing_to_return(data):
    from apps.returns.services import add_sale_line_return, complete_return, create_return

    sale = _sale_from(data, [(data["lot_b"], 2)])
    line = sale.lines.get()
    ret = create_return(source=sale, reason="Вернули всё", by=data["admin"])
    add_sale_line_return(
        ret, line, Decimal("2"), to_location=data["cell_b"],
        restock_status=StockLot.Status.AVAILABLE, by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    assert sale_cancellation_returns(sale) == []


# --- Экран подтверждения ------------------------------------------------------------------


def test_sale_confirm_screen_shows_every_cell(client, make_user, data):
    _login(client, make_user)
    sale = _sale_from(data, [(data["lot_a"], 2), (data["lot_b"], 1)])
    html = client.get(reverse("sale_cancel_confirm", args=[sale.pk])).content.decode()
    assert "Куда вернётся товар" in html
    assert "1-1-1" in html
    assert "2-3-1" in html


def test_repair_confirm_screen_shows_every_cell(client, make_user, data):
    _login(client, make_user)
    order = _repair_from(data, [(data["lot_a"], 2), (data["lot_b"], 1)])
    html = client.get(reverse("repair_order_cancel_confirm", args=[order.pk])).content.decode()
    assert "Куда вернётся товар" in html
    assert "1-1-1" in html
    assert "2-3-1" in html


def test_confirm_screen_is_read_only(client, make_user, data):
    _login(client, make_user)
    sale = _sale_from(data, [(data["lot_a"], 2)])
    before = _balances(data["bolt"])
    client.get(reverse("sale_cancel_confirm", args=[sale.pk]))
    sale.refresh_from_db()
    assert sale.status == Sale.Status.COMPLETED
    assert _balances(data["bolt"]) == before


def test_confirm_screen_explains_when_there_is_nothing_left(client, make_user, data):
    from apps.returns.services import add_sale_line_return, complete_return, create_return

    _login(client, make_user)
    sale = _sale_from(data, [(data["lot_b"], 2)])
    ret = create_return(source=sale, reason="Вернули всё", by=data["admin"])
    add_sale_line_return(
        ret, sale.lines.get(), Decimal("2"), to_location=data["cell_b"],
        restock_status=StockLot.Status.AVAILABLE, by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    html = client.get(reverse("sale_cancel_confirm", args=[sale.pk])).content.decode()
    assert "Возвращать нечего" in html


def test_repair_order_still_cancels_from_the_screen(client, make_user, data):
    _login(client, make_user)
    order = _repair_from(data, [(data["lot_a"], 1)])
    client.post(reverse("repair_order_cancel", args=[order.pk]),
                {"reason": "Ошибка", "author": "Иванов"})
    order.refresh_from_db()
    assert order.status == RepairOrder.Status.CANCELED


def test_customer_card_cancellation_path_reaches_the_preview(client, make_user, data):
    """Отмена запускается из карточки клиента, и предпросмотр там тот же."""
    _login(client, make_user)
    customer = Customer.objects.create(name="Иванов")
    sale = _sale_from(data, [(data["lot_a"], 2)])
    Sale.objects.filter(pk=sale.pk).update(customer=customer)
    html = client.get(reverse("sale_cancel_confirm", args=[sale.pk])).content.decode()
    assert "Куда вернётся товар" in html
    assert "1-1-1" in html
