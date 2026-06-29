"""Слой 20 — инвентаризация и корректировки остатков.

Покрывает план 20-layer-20-inventory-counts.md §25. Ключевое: документ сверяет факт
с системой и при расхождении приводит StockLot.quantity к факту через ADJUST_*
(физику делает inventory.adjust_stock_lot_quantity). Это НЕ списание/возврат/
продажа/ремонт. View ledger напрямую не пишет; document_type="inventory_count".
"""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    check_stock_balance,
    create_stock_lot,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.returns.models import StockReturn
from apps.sales.models import Sale
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
)
from apps.stocktaking.models import InventoryCountDocument, InventoryCountLine
from apps.stocktaking.services import (
    StocktakingError,
    add_stock_lot_count_line,
    cancel_inventory_count,
    complete_inventory_count,
    create_inventory_count,
    remove_count_line,
    update_counted_quantity,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from apps.writeoffs.models import WriteOffDocument

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
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    bulk = PartType.objects.create(
        name="Болт-Инв", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
    )
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("40"))
    bline = BatchLine.objects.create(
        batch=batch, part_type=bulk, quantity=Decimal("10"), unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)  # landed_unit 104
    bline.refresh_from_db()
    lot = create_stock_lot(bline, loc, Decimal("5"))
    receive_stock_lot(lot, by=admin)  # available, qty 5
    return {"admin": admin, "loc": loc, "lot": lot, "bline": bline}


def _counted_line(doc, lot, counted, admin):
    add_stock_lot_count_line(doc, lot, by=admin)
    line = doc.lines.get(stock_lot=lot)
    update_counted_quantity(line, Decimal(counted), by=admin)
    return line


# --- Создание / наполнение ---------------------------------------------------


def test_create_draft_count(data):
    doc = create_inventory_count(scope_location=data["loc"], by=data["admin"])
    assert doc.status == InventoryCountDocument.Status.DRAFT
    assert doc.number.startswith("IC-")


def test_cannot_complete_empty(data):
    doc = create_inventory_count(by=data["admin"])
    with pytest.raises(StocktakingError):
        complete_inventory_count(doc, by=data["admin"])


def test_add_line_snapshots_expected_and_cost(data):
    doc = create_inventory_count(by=data["admin"])
    add_stock_lot_count_line(doc, data["lot"], by=data["admin"])
    line = doc.lines.get()
    assert line.expected_quantity == Decimal("5.000")
    assert line.unit_cost_rub == Decimal("104.00")
    assert line.counted_quantity is None


def test_cannot_add_same_lot_twice(data):
    doc = create_inventory_count(by=data["admin"])
    add_stock_lot_count_line(doc, data["lot"], by=data["admin"])
    with pytest.raises(StocktakingError):
        add_stock_lot_count_line(doc, data["lot"], by=data["admin"])


# --- Проведение: совпадение / недостача / излишек ----------------------------


def test_counted_equals_live_no_movement(data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "5", data["admin"])
    complete_inventory_count(doc, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("5")
    assert not StockMovement.objects.filter(
        stock_lot=data["lot"],
        movement_type__in=[
            StockMovement.MovementType.ADJUST_IN, StockMovement.MovementType.ADJUST_OUT
        ],
    ).exists()
    assert doc.lines.get().adjustment is None
    doc.refresh_from_db()
    assert doc.status == InventoryCountDocument.Status.COMPLETED


def test_counted_less_creates_adjust_out(data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "3", data["admin"])
    complete_inventory_count(doc, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("3")
    mv = StockMovement.objects.get(
        stock_lot=data["lot"], movement_type=StockMovement.MovementType.ADJUST_OUT
    )
    assert mv.quantity == Decimal("2")
    assert mv.from_location_id == data["loc"].pk
    assert mv.to_location_id is None
    assert mv.document_type == "inventory_count"
    assert mv.document_id == doc.pk
    assert doc.lines.get().adjustment_id == mv.pk  # строка хранит ссылку


def test_counted_more_creates_adjust_in(data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "8", data["admin"])
    complete_inventory_count(doc, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("8")
    mv = StockMovement.objects.get(
        stock_lot=data["lot"], movement_type=StockMovement.MovementType.ADJUST_IN
    )
    assert mv.quantity == Decimal("3")
    assert mv.from_location_id is None
    assert mv.to_location_id == data["loc"].pk
    assert mv.document_type == "inventory_count"


def test_counted_zero_depletes_lot(data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "0", data["admin"])
    complete_inventory_count(doc, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("0")
    assert data["lot"].status == StockLot.Status.DEPLETED


# --- Инварианты --------------------------------------------------------------


def test_cannot_count_negative(data):
    doc = create_inventory_count(by=data["admin"])
    add_stock_lot_count_line(doc, data["lot"], by=data["admin"])
    line = doc.lines.get()
    with pytest.raises(StocktakingError):
        update_counted_quantity(line, Decimal("-1"), by=data["admin"])


def test_cannot_reduce_below_reserved(data):
    r = create_reservation(customer_name="Бронь", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("4"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "3", data["admin"])  # 3 < бронь 4
    with pytest.raises(StocktakingError):
        complete_inventory_count(doc, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("5")  # откат


def test_cannot_complete_with_uncounted_line(data):
    doc = create_inventory_count(by=data["admin"])
    add_stock_lot_count_line(doc, data["lot"], by=data["admin"])  # counted не введён
    with pytest.raises(StocktakingError):
        complete_inventory_count(doc, by=data["admin"])


def test_completed_is_immutable(data):
    doc = create_inventory_count(by=data["admin"])
    line = _counted_line(doc, data["lot"], "4", data["admin"])
    complete_inventory_count(doc, by=data["admin"])
    with pytest.raises(StocktakingError):
        complete_inventory_count(doc, by=data["admin"])
    with pytest.raises(StocktakingError):
        update_counted_quantity(line, Decimal("2"), by=data["admin"])
    with pytest.raises(StocktakingError):
        remove_count_line(line, by=data["admin"])
    with pytest.raises(StocktakingError):
        cancel_inventory_count(doc, by=data["admin"])


def test_check_stock_balance_green_after_complete(data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "3", data["admin"])
    complete_inventory_count(doc, by=data["admin"])
    assert check_stock_balance() == []  # кэш = первичка


# --- Границы: инвентаризация — не другой документ ----------------------------


def test_count_does_not_create_other_documents(data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "3", data["admin"])
    complete_inventory_count(doc, by=data["admin"])
    assert Sale.objects.count() == 0
    assert RepairOrder.objects.count() == 0
    assert StockReturn.objects.count() == 0
    assert WriteOffDocument.objects.count() == 0


# --- Права / себестоимость ----------------------------------------------------


def test_list_requires_login(client):
    assert client.get(reverse("inventory_count_list")).status_code == 302


def test_storekeeper_can_complete(make_user, client, data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "4", data["admin"])
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("inventory_count_complete", args=[doc.pk]))
    assert resp.status_code == 302
    doc.refresh_from_db()
    assert doc.status == InventoryCountDocument.Status.COMPLETED


def test_seller_cannot_complete(make_user, client, data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "4", data["admin"])
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("inventory_count_list")).status_code == 200  # просмотр ок
    assert client.post(reverse("inventory_count_complete", args=[doc.pk])).status_code == 403
    doc.refresh_from_db()
    assert doc.status == InventoryCountDocument.Status.DRAFT


def test_cost_hidden_without_capability(make_user, client, data):
    doc = create_inventory_count(by=data["admin"])
    add_stock_lot_count_line(doc, data["lot"], by=data["admin"])
    make_user("sklad", role=roles.STOREKEEPER)  # manage_stocktaking, но не purchase_cost
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("inventory_count_detail", args=[doc.pk])).content.decode()
    assert "Себестоимость" not in html


def test_cost_visible_for_manager(make_user, client, data):
    doc = create_inventory_count(by=data["admin"])
    add_stock_lot_count_line(doc, data["lot"], by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("inventory_count_detail", args=[doc.pk])).content.decode()
    assert "Себестоимость" in html


# --- Архитектура: view не пишет ledger ---------------------------------------


def test_view_delegates_without_writing_ledger(make_user, client, data):
    doc = create_inventory_count(by=data["admin"])
    _counted_line(doc, data["lot"], "3", data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    m_before = StockMovement.objects.count()
    with patch("apps.stocktaking.views.complete_inventory_count") as mock_complete:
        client.post(reverse("inventory_count_complete", args=[doc.pk]))
    mock_complete.assert_called_once()
    assert StockMovement.objects.count() == m_before  # вьюха движений не пишет


def test_untrusted_params_rechecked(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Несуществующий документ → 404.
    assert client.post(reverse("inventory_count_complete", args=[999999])).status_code == 404
    # Подмена лота на несуществующий id в add-lot → ошибка формы, без эффекта.
    doc = create_inventory_count(by=data["admin"])
    resp = client.post(reverse("inventory_count_add_lot", args=[doc.pk]), {"lot": 999999})
    assert resp.status_code == 302
    assert not InventoryCountLine.objects.filter(count_document=doc).exists()
