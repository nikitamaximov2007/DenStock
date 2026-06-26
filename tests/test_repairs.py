"""Слой 17 — выдача деталей в ремонт / установка на технику.

Покрывает план 17-layer-17-repair-issue-installation.md §23. Ключевое: выдача в
ремонт — окончательный складской расход (StockMovement ISSUE_*, статус/количество
меняются, себестоимость заморожена), но НЕ продажа/оплата/чек. Физику делают
inventory.issue_*, документ ведёт apps/repairs; view ledger напрямую не пишет.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit, VehicleType
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    RepairError,
    add_part_item_to_repair_order,
    add_stock_lot_to_repair_order,
    cancel_repair_order,
    complete_repair_order,
    create_repair_order,
    remove_repair_line,
)
from apps.sales.models import Sale
from apps.sales.services import (
    activate_reservation,
    add_part_item_to_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
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
    vtype, _ = VehicleType.objects.get_or_create(name="Снегоход")

    serial = PartType.objects.create(
        name="Насос-Ремонт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("500"), min_price=Decimal("400"),
    )
    iline = _finalized_line(sup, serial, admin, qty="2")  # landed_unit 120
    item = create_part_items(iline, 1, serial_number="SN-REP-1")[0]
    receive_part_item(item, to_location=loc, by=admin)  # available @ loc
    item_receiving = create_part_items(iline, 1, serial_number="SN-REP-2")[0]

    bulk = PartType.objects.create(
        name="Болт-Ремонт", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
    )
    bline = _finalized_line(sup, bulk, admin, qty="10")  # landed_unit 104
    lot = create_stock_lot(bline, loc, Decimal("5"))
    receive_stock_lot(lot, by=admin)  # available @ loc, qty 5

    return {
        "admin": admin, "serial": serial, "item": item, "item_receiving": item_receiving,
        "bulk": bulk, "lot": lot, "loc": loc, "vtype": vtype,
    }


# --- Создание / проведение ----------------------------------------------------


def test_create_draft_repair_order(data):
    order = create_repair_order(
        customer_name="Иван", vehicle_type=data["vtype"], by=data["admin"]
    )
    assert order.status == RepairOrder.Status.DRAFT
    assert order.number.startswith("R-")


def test_cannot_complete_empty_order(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    with pytest.raises(RepairError):
        complete_repair_order(order, by=data["admin"])
    order.refresh_from_db()
    assert order.status == RepairOrder.Status.DRAFT


def test_add_part_item_to_order(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    line = add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    assert line.part_item_id == data["item"].pk
    assert line.quantity == Decimal("1")


# --- Выдача PartItem ----------------------------------------------------------


def test_complete_issues_part_item(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    data["item"].refresh_from_db()
    order.refresh_from_db()
    assert data["item"].status == PartItem.Status.INSTALLED
    assert order.status == RepairOrder.Status.COMPLETED
    assert order.completed_at is not None


def test_issued_part_item_not_available(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    # Единственный экземпляр выдан → строка кэша по его batch_line/ячейке удалена.
    assert not StockBalance.objects.filter(
        batch_line=data["item"].batch_line, location=data["loc"]
    ).exists()


def test_issue_creates_movement_issue_item(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    mv = StockMovement.objects.get(
        movement_type=StockMovement.MovementType.ISSUE_ITEM, part_item=data["item"]
    )
    assert mv.from_location_id == data["loc"].pk
    assert mv.to_location_id is None
    assert mv.quantity == Decimal("1")
    assert mv.document_type == "repair_order"
    assert mv.document_id == order.pk


def test_repair_line_freezes_cost(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    line = order.lines.get()
    assert line.unit_cost_rub == Decimal("120.00")     # landed cost заморожена
    assert line.total_cost_rub == Decimal("120.00")
    assert line.issued_at is not None
    order.refresh_from_db()
    assert order.cost_total == Decimal("120.00")


# --- Выдача StockLot ----------------------------------------------------------


def test_issue_stock_lot_quantity(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_repair_order(order, data["lot"], Decimal("2"), by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("3")  # 5 − 2 выданных
    bal = StockBalance.objects.get(batch_line=data["lot"].batch_line, location=data["loc"])
    assert bal.quantity_physical == Decimal("3")
    line = order.lines.get()
    assert line.unit_cost_rub == Decimal("104.00")
    assert line.total_cost_rub == Decimal("208.00")


def test_stock_lot_depleted_at_zero(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_repair_order(order, data["lot"], Decimal("5"), by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("0")
    assert data["lot"].status == StockLot.Status.DEPLETED
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.ISSUE_LOT, stock_lot=data["lot"]
    ).exists()


def test_cannot_issue_lot_over_available(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    with pytest.raises(RepairError):
        add_stock_lot_to_repair_order(order, data["lot"], Decimal("6"), by=data["admin"])


# --- Инварианты статуса / резерва --------------------------------------------


def test_cannot_issue_receiving(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    with pytest.raises(RepairError):
        add_part_item_to_repair_order(order, data["item_receiving"], by=data["admin"])


def test_cannot_issue_reserved(data):
    r = create_reservation(customer_name="Бронь", by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    activate_reservation(r, by=data["admin"])
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    with pytest.raises(RepairError):
        add_part_item_to_repair_order(order, data["item"], by=data["admin"])


def test_cannot_issue_reserved_lot_quantity(data):
    r = create_reservation(customer_name="Бронь", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("4"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    # Доступно для выдачи 5 − 4 (резерв) = 1; запрос 2 → ошибка.
    with pytest.raises(RepairError):
        add_stock_lot_to_repair_order(order, data["lot"], Decimal("2"), by=data["admin"])


def test_cannot_issue_already_installed(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])  # item → installed
    order2 = create_repair_order(customer_name="Пётр", by=data["admin"])
    with pytest.raises(RepairError):
        add_part_item_to_repair_order(order2, data["item"], by=data["admin"])


def test_completed_order_is_immutable(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    line = add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    with pytest.raises(RepairError):
        complete_repair_order(order, by=data["admin"])
    with pytest.raises(RepairError):
        remove_repair_line(line, by=data["admin"])
    with pytest.raises(RepairError):
        cancel_repair_order(order, by=data["admin"])


# --- Границы: выдача — не продажа / не оплата ---------------------------------


def test_issue_does_not_create_sale(data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    assert Sale.objects.count() == 0


def test_repair_order_is_not_payment(data):
    field_names = {f.name for f in RepairOrder._meta.get_fields()}
    forbidden = {
        "paid", "payment", "payment_method", "receipt", "cash", "card", "is_paid",
        "price", "revenue", "profit", "sale",
    }
    assert field_names.isdisjoint(forbidden)


# --- Права / себестоимость ----------------------------------------------------


def test_repair_list_requires_login(client):
    assert client.get(reverse("repair_order_list")).status_code == 302


def test_storekeeper_can_create_repair(make_user, client, data):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(
        reverse("repair_order_create"), {"customer_name": "Клиент"}
    )
    assert resp.status_code == 302
    assert RepairOrder.objects.filter(customer_name="Клиент").exists()


def test_seller_can_create_repair(make_user, client, data):
    make_user("master", role=roles.SELLER)
    client.login(username="master", password=PASSWORD)
    resp = client.post(reverse("repair_order_create"), {"customer_name": "Клиент2"})
    assert resp.status_code == 302
    assert RepairOrder.objects.filter(customer_name="Клиент2").exists()


def test_viewer_cannot_create_repair(make_user, client, data):
    make_user("nablyudatel", role=roles.VIEWER)
    client.login(username="nablyudatel", password=PASSWORD)
    assert client.get(reverse("repair_order_list")).status_code == 200  # просмотр ок
    assert client.post(
        reverse("repair_order_create"), {"customer_name": "X"}
    ).status_code == 403


def test_cost_hidden_without_capability(make_user, client, data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    make_user("master", role=roles.SELLER)  # имеет manage_repairs, но не purchase_cost
    client.login(username="master", password=PASSWORD)
    html = client.get(reverse("repair_order_detail", args=[order.pk])).content.decode()
    assert "Себестоимость" not in html


def test_cost_visible_for_manager(make_user, client, data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    complete_repair_order(order, by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("repair_order_detail", args=[order.pk])).content.decode()
    assert "Себестоимость" in html


# --- Архитектура: view не пишет ledger ---------------------------------------


def test_view_delegates_to_service_without_writing_ledger(make_user, client, data):
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    add_part_item_to_repair_order(order, data["item"], by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    m_before = StockMovement.objects.count()
    b_before = sorted(StockBalance.objects.values_list("id", "quantity_physical"))
    with patch("apps.repairs.views.complete_repair_order") as mock_complete:
        client.post(reverse("repair_order_complete", args=[order.pk]))
    mock_complete.assert_called_once()
    assert StockMovement.objects.count() == m_before          # вьюха движений не пишет
    assert sorted(StockBalance.objects.values_list("id", "quantity_physical")) == b_before


def test_untrusted_params_rechecked(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Несуществующий заказ → 404.
    assert client.post(reverse("repair_order_complete", args=[999999])).status_code == 404
    # Подмена лота на несуществующий id в add-lot → ошибка формы, без эффекта.
    order = create_repair_order(customer_name="Иван", by=data["admin"])
    resp = client.post(
        reverse("repair_order_add_lot", args=[order.pk]),
        {"lot": 999999, "quantity": "1"},
    )
    assert resp.status_code == 302
    assert not order.lines.exists()
