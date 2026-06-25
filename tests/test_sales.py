"""Слой 16 — продажи (коммерческий документ + складской расход).

Покрывает план 16-layer-16-sales.md §21. Ключевое: продажа создаёт физический
расход (StockMovement SALE_*, статус/количество меняются), фиксирует выручку/
себестоимость/прибыль — но НЕ становится кассой/чеком. Физику делают
inventory.sell_*, документ ведёт apps/sales; view ledger напрямую не пишет.
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
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Reservation, Sale
from apps.sales.services import (
    SaleError,
    activate_reservation,
    add_part_item_to_reservation,
    add_part_item_to_sale,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    complete_sale,
    create_reservation,
    create_sale,
    create_sale_from_reservation,
    remove_sale_line,
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

    serial = PartType.objects.create(
        name="Насос-Продажа", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("500"), min_price=Decimal("400"),
    )
    iline = _finalized_line(sup, serial, admin, qty="2")  # landed_unit 120
    item = create_part_items(iline, 1, serial_number="SN-SALE-1")[0]
    receive_part_item(item, to_location=loc, by=admin)  # available @ loc
    item_receiving = create_part_items(iline, 1, serial_number="SN-SALE-2")[0]

    bulk = PartType.objects.create(
        name="Болт-Продажа", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("200"),
    )
    bline = _finalized_line(sup, bulk, admin, qty="10")  # landed_unit 104
    lot = create_stock_lot(bline, loc, Decimal("5"))
    receive_stock_lot(lot, by=admin)  # available @ loc, qty 5

    return {
        "admin": admin, "serial": serial, "item": item, "item_receiving": item_receiving,
        "bulk": bulk, "lot": lot, "loc": loc,
    }


# --- Создание / завершение ---------------------------------------------------


def test_create_draft_sale(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    assert sale.status == Sale.Status.DRAFT
    assert sale.number.startswith("S-")


def test_cannot_complete_empty_sale(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    with pytest.raises(SaleError):
        complete_sale(sale, by=data["admin"])
    sale.refresh_from_db()
    assert sale.status == Sale.Status.DRAFT


def test_add_part_item_to_sale(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    line = add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    assert line.part_item_id == data["item"].pk
    assert line.total_price == Decimal("500.00")


# --- Продажа PartItem --------------------------------------------------------


def test_complete_sale_sells_part_item(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    complete_sale(sale, by=data["admin"])
    data["item"].refresh_from_db()
    sale.refresh_from_db()
    assert data["item"].status == PartItem.Status.SOLD
    assert sale.status == Sale.Status.COMPLETED
    assert sale.sold_at is not None


def test_sold_part_item_not_available(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    complete_sale(sale, by=data["admin"])
    # Единственный экземпляр продан → строка кэша по его batch_line/ячейке удалена.
    assert not StockBalance.objects.filter(
        batch_line=data["item"].batch_line, location=data["loc"]
    ).exists()


def test_sale_creates_movement_sale_item(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    complete_sale(sale, by=data["admin"])
    mv = StockMovement.objects.get(
        movement_type=StockMovement.MovementType.SALE_ITEM, part_item=data["item"]
    )
    assert mv.from_location_id == data["loc"].pk
    assert mv.to_location_id is None
    assert mv.quantity == Decimal("1")
    assert mv.document_type == "sale"
    assert mv.document_id == sale.pk


def test_saleline_freezes_cost_and_profit(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    complete_sale(sale, by=data["admin"])
    line = sale.lines.get()
    assert line.unit_cost_rub == Decimal("120.00")     # landed cost заморожена
    assert line.total_cost_rub == Decimal("120.00")
    assert line.profit_rub == Decimal("380.00")        # 500 − 120
    sale.refresh_from_db()
    assert sale.revenue_total == Decimal("500.00")
    assert sale.profit_total == Decimal("380.00")


# --- Продажа StockLot --------------------------------------------------------


def test_sell_stock_lot_quantity(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_sale(
        sale, data["lot"], Decimal("2"), unit_price=Decimal("200"), by=data["admin"]
    )
    complete_sale(sale, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("3")  # 5 − 2 проданных
    bal = StockBalance.objects.get(batch_line=data["lot"].batch_line, location=data["loc"])
    assert bal.quantity_physical == Decimal("3")


def test_stock_lot_depleted_at_zero(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_sale(
        sale, data["lot"], Decimal("5"), unit_price=Decimal("200"), by=data["admin"]
    )
    complete_sale(sale, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("0")
    assert data["lot"].status == StockLot.Status.DEPLETED
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.SALE_LOT, stock_lot=data["lot"]
    ).exists()


def test_cannot_sell_lot_over_available(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    with pytest.raises(SaleError):
        add_stock_lot_to_sale(
            sale, data["lot"], Decimal("6"), unit_price=Decimal("200"), by=data["admin"]
        )


def test_cannot_sell_receiving(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    with pytest.raises(SaleError):
        add_part_item_to_sale(
            sale, data["item_receiving"], unit_price=Decimal("500"), by=data["admin"]
        )


# --- Взаимодействие с резервами ----------------------------------------------


def test_cannot_sell_reserved_by_other(data):
    r = create_reservation(customer_name="Бронь", by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    activate_reservation(r, by=data["admin"])
    sale = create_sale(customer_name="Иван", by=data["admin"])  # без резерва
    with pytest.raises(SaleError):
        add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])


def test_create_and_complete_sale_from_reservation(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("2"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    sale = create_sale_from_reservation(r, by=data["admin"])
    assert sale.reservation_id == r.pk
    assert sale.lines.count() == 1
    complete_sale(sale, by=data["admin"])
    r.refresh_from_db()
    data["lot"].refresh_from_db()
    assert r.status == Reservation.Status.CONVERTED
    assert data["lot"].quantity == Decimal("3")  # 5 − 2 проданных
    bal = StockBalance.objects.get(batch_line=data["lot"].batch_line, location=data["loc"])
    assert bal.quantity_reserved == Decimal("0")   # резерв освобождён
    assert bal.quantity_available == Decimal("3")


# --- Иммутабельность ---------------------------------------------------------


def test_completed_sale_is_immutable(data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    line = add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    complete_sale(sale, by=data["admin"])
    with pytest.raises(SaleError):
        complete_sale(sale, by=data["admin"])
    with pytest.raises(SaleError):
        remove_sale_line(line, by=data["admin"])


# --- Границы: продажа — не оплата/чек ----------------------------------------


def test_sale_is_not_payment(data):
    field_names = {f.name for f in Sale._meta.get_fields()}
    forbidden = {"paid", "payment", "payment_method", "receipt", "cash", "card", "is_paid"}
    assert field_names.isdisjoint(forbidden)


# --- Права / себестоимость ----------------------------------------------------


def test_sale_list_requires_login(client):
    assert client.get(reverse("sale_list")).status_code == 302


def test_seller_can_create_sale(make_user, client, data):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    resp = client.post(reverse("sale_create"), {"customer_name": "Клиент"})
    assert resp.status_code == 302
    assert Sale.objects.filter(customer_name="Клиент").exists()


def test_storekeeper_cannot_create_sale(make_user, client, data):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    assert client.get(reverse("sale_list")).status_code == 200  # просмотр ок
    assert client.post(reverse("sale_create"), {"customer_name": "X"}).status_code == 403


def test_cost_profit_hidden_without_capability(make_user, client, data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    complete_sale(sale, by=data["admin"])
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    html = client.get(reverse("sale_detail", args=[sale.pk])).content.decode()
    assert "Выручка" in html          # выручку продавец видит
    assert "Прибыль" not in html      # прибыль/себестоимость — нет


def test_cost_profit_visible_for_manager(make_user, client, data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    complete_sale(sale, by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("sale_detail", args=[sale.pk])).content.decode()
    assert "Прибыль" in html


# --- Архитектура: view не пишет ledger ---------------------------------------


def test_view_delegates_to_service_without_writing_ledger(make_user, client, data):
    sale = create_sale(customer_name="Иван", by=data["admin"])
    add_part_item_to_sale(sale, data["item"], unit_price=Decimal("500"), by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    m_before = StockMovement.objects.count()
    b_before = sorted(StockBalance.objects.values_list("id", "quantity_physical"))
    with patch("apps.sales.views.complete_sale") as mock_complete:
        client.post(reverse("sale_complete", args=[sale.pk]))
    mock_complete.assert_called_once()
    assert StockMovement.objects.count() == m_before          # вьюха движений не пишет
    assert sorted(StockBalance.objects.values_list("id", "quantity_physical")) == b_before


def test_untrusted_params_rechecked(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Несуществующая продажа → 404.
    assert client.post(reverse("sale_complete", args=[999999])).status_code == 404
    # Подмена лота на несуществующий id в add-lot → ошибка формы, без эффекта.
    sale = create_sale(customer_name="Иван", by=data["admin"])
    resp = client.post(
        reverse("sale_add_lot", args=[sale.pk]),
        {"lot": 999999, "quantity": "1", "unit_price": "100"},
    )
    assert resp.status_code == 302
    assert not sale.lines.exists()
