"""Слой 18 — возвраты на склад (физическое обратное поступление).

Покрывает план 18-layer-18-stock-returns.md §22. Ключевое: возврат восстанавливает
физический остаток (StockMovement RETURN_*, статус/количество растут) и фиксирует
себестоимость из источника — но НЕ делает денежный refund и НЕ меняет
Sale/RepairOrder. Физику делают inventory.return_*, документ ведёт apps/returns;
view ledger напрямую не пишет.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    add_part_item_to_repair_order,
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.models import StockReturn
from apps.returns.services import (
    ReturnError,
    add_repair_line_return,
    add_sale_line_return,
    complete_return,
    create_return,
    remove_return_line,
)
from apps.sales.models import Sale
from apps.sales.services import (
    add_part_item_to_sale,
    add_stock_lot_to_sale,
    complete_sale,
    create_sale,
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


def _finalized_line(sup, part, admin, *, qty, unit_cost="100", shipping="40"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal(shipping))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(qty), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка B", code="B-01", storage_allowed=True, is_active=True
    )
    bad_loc = StorageLocation.objects.create(
        name="Зона приёмки", code="RECV", storage_allowed=False, is_active=True
    )
    inactive_loc = StorageLocation.objects.create(
        name="Архив", code="ARCH", storage_allowed=True, is_active=False
    )

    serial = PartType.objects.create(
        name="Насос-Возврат", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("500"), min_price=Decimal("400"),
    )
    iline = _finalized_line(sup, serial, admin, qty="2")  # landed_unit 120
    item_a = create_part_items(iline, 1, serial_number="SN-RET-A")[0]
    receive_part_item(item_a, to_location=loc, by=admin)
    item_b = create_part_items(iline, 1, serial_number="SN-RET-B")[0]
    receive_part_item(item_b, to_location=loc, by=admin)

    bulk = PartType.objects.create(
        name="Болт-Возврат", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
    )
    bline = _finalized_line(sup, bulk, admin, qty="10")  # landed_unit 104
    lot = create_stock_lot(bline, loc, Decimal("10"))
    receive_stock_lot(lot, by=admin)

    small = PartType.objects.create(
        name="Шайба-Возврат", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
    )
    sline = _finalized_line(sup, small, admin, qty="4")
    lot_small = create_stock_lot(sline, loc, Decimal("2"))
    receive_stock_lot(lot_small, by=admin)

    # Продажа: экземпляр item_a (1), 3 из lot, весь lot_small (2 → depleted).
    sale = create_sale(customer_name="Покупатель", by=admin)
    add_part_item_to_sale(sale, item_a, unit_price=Decimal("500"), by=admin)
    add_stock_lot_to_sale(sale, lot, Decimal("3"), unit_price=Decimal("200"), by=admin)
    add_stock_lot_to_sale(sale, lot_small, Decimal("2"), unit_price=Decimal("50"), by=admin)
    complete_sale(sale, by=admin)
    sale.refresh_from_db()

    # Ремонт: экземпляр item_b + 2 из lot.
    order = create_repair_order(customer_name="Клиент", by=admin)
    add_part_item_to_repair_order(order, item_b, by=admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("2"), by=admin)
    complete_repair_order(order, by=admin)
    order.refresh_from_db()

    return {
        "admin": admin, "loc": loc, "loc2": loc2, "bad_loc": bad_loc,
        "inactive_loc": inactive_loc, "item_a": item_a, "item_b": item_b,
        "lot": lot, "lot_small": lot_small, "sale": sale, "order": order,
        "sale_item_line": sale.lines.get(part_item=item_a),
        "sale_lot_line": sale.lines.get(stock_lot=lot),
        "sale_small_line": sale.lines.get(stock_lot=lot_small),
        "repair_item_line": order.lines.get(part_item=item_b),
        "repair_lot_line": order.lines.get(stock_lot=lot),
    }


def _new_return(data, source):
    return create_return(source=source, by=data["admin"])


# --- Создание / проведение ----------------------------------------------------


def test_create_draft_return(data):
    ret = _new_return(data, data["sale"])
    assert ret.status == StockReturn.Status.DRAFT
    assert ret.number.startswith("RET-")
    assert ret.source_type == StockReturn.SourceType.SALE


def test_cannot_complete_empty_return(data):
    ret = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        complete_return(ret, by=data["admin"])
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.DRAFT


def test_cannot_return_from_draft_sale(data):
    draft_sale = create_sale(customer_name="Черновик", by=data["admin"])
    with pytest.raises(ReturnError):
        create_return(source=draft_sale, by=data["admin"])


# --- Возврат проданного PartItem ---------------------------------------------


def test_return_sold_part_item_quarantine(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["item_a"].refresh_from_db()
    assert data["item_a"].status == PartItem.Status.QUARANTINE
    assert data["item_a"].current_location_id == data["loc"].pk
    mv = StockMovement.objects.get(
        movement_type=StockMovement.MovementType.RETURN_ITEM, part_item=data["item_a"]
    )
    assert mv.from_location_id is None
    assert mv.to_location_id == data["loc"].pk
    assert mv.quantity == Decimal("1")
    assert mv.document_type == "stock_return"
    assert mv.document_id == ret.pk


def test_return_sold_part_item_available_explicit(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["item_a"].refresh_from_db()
    assert data["item_a"].status == PartItem.Status.AVAILABLE
    bal = StockBalance.objects.get(batch_line=data["item_a"].batch_line, location=data["loc"])
    assert bal.quantity_available >= Decimal("1")


def test_return_increases_balance(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    bal = StockBalance.objects.get(batch_line=data["item_a"].batch_line, location=data["loc"])
    assert bal.quantity_physical == Decimal("1")
    assert bal.quantity_quarantine == Decimal("1")
    assert bal.quantity_available == Decimal("0")  # карантин не в доступном


# --- Возврат выданного в ремонт PartItem -------------------------------------


def test_return_installed_part_item(data):
    ret = _new_return(data, data["order"])
    assert ret.source_type == StockReturn.SourceType.REPAIR_ORDER
    add_repair_line_return(
        ret, data["repair_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["item_b"].refresh_from_db()
    assert data["item_b"].status == PartItem.Status.QUARANTINE
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_ITEM, part_item=data["item_b"]
    ).exists()


# --- Возврат StockLot quantity -----------------------------------------------


def test_return_sold_stock_lot_quantity(data):
    data["lot"].refresh_from_db()
    before = data["lot"].quantity  # 5 (10 − 3 продажа − 2 ремонт)
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == before + Decimal("2")
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_LOT, stock_lot=data["lot"]
    ).exists()


def test_return_repair_stock_lot_quantity(data):
    data["lot"].refresh_from_db()
    before = data["lot"].quantity
    ret = _new_return(data, data["order"])
    add_repair_line_return(
        ret, data["repair_lot_line"], Decimal("1"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == before + Decimal("1")


def test_depleted_lot_revived_on_return(data):
    data["lot_small"].refresh_from_db()
    assert data["lot_small"].status == StockLot.Status.DEPLETED  # продан целиком
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_small_line"], Decimal("2"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["lot_small"].refresh_from_db()
    assert data["lot_small"].status == StockLot.Status.AVAILABLE
    assert data["lot_small"].quantity == Decimal("2")


def test_new_lot_when_returning_to_empty_cell(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc2"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    new_lot = StockLot.objects.get(batch_line=data["lot"].batch_line, location=data["loc2"])
    assert new_lot.pk != data["lot"].pk
    assert new_lot.quantity == Decimal("2")


def test_cannot_mix_quarantine_and_available_in_cell(data):
    # В loc уже есть available-лот (data["lot"]). Возврат в loc как quarantine → конфликт.
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    with pytest.raises(ReturnError):
        complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    # Откат: количество не изменилось.
    assert data["lot"].quantity == Decimal("5")


# --- Инварианты «не больше / не дважды / ячейка» -----------------------------


def test_cannot_return_more_than_sold(data):
    ret = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret, data["sale_lot_line"], Decimal("4"),  # продано всего 3
            to_location=data["loc"], restock_status="available", by=data["admin"],
        )


def test_cannot_return_item_twice(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    ret2 = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret2, data["sale_item_line"], Decimal("1"),
            to_location=data["loc"], restock_status="quarantine", by=data["admin"],
        )


def test_cannot_complete_return_twice(data):
    ret = _new_return(data, data["sale"])
    line = add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    with pytest.raises(ReturnError):
        complete_return(ret, by=data["admin"])
    with pytest.raises(ReturnError):
        remove_return_line(line, by=data["admin"])


def test_cannot_return_to_storage_not_allowed(data):
    ret = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret, data["sale_item_line"], Decimal("1"),
            to_location=data["bad_loc"], restock_status="quarantine", by=data["admin"],
        )


def test_cannot_return_to_inactive_location(data):
    ret = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret, data["sale_item_line"], Decimal("1"),
            to_location=data["inactive_loc"], restock_status="quarantine", by=data["admin"],
        )


def test_return_part_item_service_rejects_available(data):
    # Прямой вызов физического сервиса на доступном экземпляре — нельзя вернуть.
    from apps.inventory.services import return_part_item

    available_item = data["item_a"]  # ещё sold
    return_part_item(
        available_item, data["loc"], restock_status="quarantine", by=data["admin"]
    )
    available_item.refresh_from_db()
    with pytest.raises(InventoryError):
        return_part_item(
            available_item, data["loc"], restock_status="available", by=data["admin"]
        )


# --- Себестоимость -----------------------------------------------------------


def test_return_freezes_cost_from_source(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc2"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    line = ret.lines.get()
    assert line.unit_cost_rub == Decimal("104.00")          # из SaleLine, не текущий landed
    assert line.total_cost_rub == Decimal("208.00")
    ret.refresh_from_db()
    assert ret.cost_total == Decimal("208.00")


# --- Границы: возврат — не продажа / не оплата / не сторно --------------------


def test_return_does_not_create_sale(data):
    sales_before = Sale.objects.count()
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    assert Sale.objects.count() == sales_before


def test_return_is_not_payment(data):
    field_names = {f.name for f in StockReturn._meta.get_fields()}
    forbidden = {"paid", "payment", "payment_method", "receipt", "cash", "card", "refund"}
    assert field_names.isdisjoint(forbidden)


def test_sale_and_repair_unchanged_after_return(data):
    sale_revenue = data["sale"].revenue_total
    sale_profit = data["sale"].profit_total
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["sale"].refresh_from_db()
    data["order"].refresh_from_db()
    assert data["sale"].status == Sale.Status.COMPLETED
    assert data["sale"].revenue_total == sale_revenue
    assert data["sale"].profit_total == sale_profit
    assert data["order"].status == RepairOrder.Status.COMPLETED


# --- Права / себестоимость ----------------------------------------------------


def test_return_list_requires_login(client):
    assert client.get(reverse("return_list")).status_code == 302


def test_storekeeper_can_complete_return(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("return_complete", args=[ret.pk]))
    assert resp.status_code == 302
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED


def test_seller_cannot_complete_return(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("return_list")).status_code == 200  # просмотр ок
    assert client.post(reverse("return_complete", args=[ret.pk])).status_code == 403
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.DRAFT


def test_cost_hidden_without_capability(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    make_user("sklad", role=roles.STOREKEEPER)  # имеет manage_returns, но не purchase_cost
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("return_detail", args=[ret.pk])).content.decode()
    assert "Себестоимость" not in html


def test_cost_visible_for_manager(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("return_detail", args=[ret.pk])).content.decode()
    assert "Себестоимость" in html


# --- Архитектура: view не пишет ledger ---------------------------------------


def test_view_delegates_to_service_without_writing_ledger(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    m_before = StockMovement.objects.count()
    b_before = sorted(StockBalance.objects.values_list("id", "quantity_physical"))
    with patch("apps.returns.views.complete_return") as mock_complete:
        client.post(reverse("return_complete", args=[ret.pk]))
    mock_complete.assert_called_once()
    assert StockMovement.objects.count() == m_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_physical")) == b_before


def test_untrusted_params_rechecked(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Несуществующий возврат → 404.
    assert client.post(reverse("return_complete", args=[999999])).status_code == 404
    # Подмена строки-источника на несуществующий id → ошибка, без эффекта.
    ret = _new_return(data, data["sale"])
    resp = client.post(
        reverse("return_add_line", args=[ret.pk]),
        {"source_line_id": 999999, "to_location": data["loc"].pk,
         "restock_status": "quarantine", "quantity": "1"},
    )
    assert resp.status_code == 302
    assert not ret.lines.exists()
