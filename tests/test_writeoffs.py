"""Слой 19 — документированные списания (брак/потеря/утилизация).

Покрывает план 19-layer-19-write-offs.md §20. Ключевое: списание — окончательное
складское выбытие (StockMovement WRITE_OFF_*, статус/количество уменьшаются,
себестоимость заморожена), но НЕ продажа/ремонт/возврат/инвентаризация/оплата.
Физику делают inventory.write_off_*, документ ведёт apps/writeoffs; view ledger
напрямую не пишет.
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
    recompute_balance_row,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.returns.models import StockReturn
from apps.sales.models import Sale
from apps.sales.services import (
    activate_reservation,
    add_part_item_to_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from apps.writeoffs.models import WriteOffDocument, WriteOffLine
from apps.writeoffs.services import (
    WriteOffError,
    add_part_item_to_write_off,
    add_stock_lot_to_write_off,
    cancel_write_off,
    complete_write_off,
    create_write_off,
    remove_write_off_line,
)

PASSWORD = "parol-12345"
R = WriteOffDocument.Reason


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
        name="Насос-Списание", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
    )
    # Каждый экземпляр — со своей строки партии (чтобы баланс не сцеплялся).
    il_a = _finalized_line(sup, serial, admin, qty="2")  # landed 120
    item_a = create_part_items(il_a, 1, serial_number="SN-WO-A")[0]
    receive_part_item(item_a, to_location=loc, by=admin)  # available @ loc

    il_q = _finalized_line(sup, serial, admin, qty="2")
    item_q = create_part_items(il_q, 1, serial_number="SN-WO-Q")[0]
    receive_part_item(item_q, to_location=loc, by=admin)
    item_q.status = PartItem.Status.QUARANTINE  # арранж: карантин
    item_q.save(update_fields=["status"])
    recompute_balance_row(il_q, loc)

    il_r = _finalized_line(sup, serial, admin, qty="2")
    item_recv = create_part_items(il_r, 1, serial_number="SN-WO-R")[0]  # receiving

    il_s = _finalized_line(sup, serial, admin, qty="2")
    item_sold = create_part_items(il_s, 1, serial_number="SN-WO-S")[0]
    receive_part_item(item_sold, to_location=loc, by=admin)
    item_sold.status = PartItem.Status.SOLD  # арранж: продан
    item_sold.save(update_fields=["status"])
    recompute_balance_row(il_s, loc)

    bulk = PartType.objects.create(
        name="Болт-Списание", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
    )
    bl = _finalized_line(sup, bulk, admin, qty="10")  # landed 104
    lot = create_stock_lot(bl, loc, Decimal("5"))
    receive_stock_lot(lot, by=admin)  # available @ loc, qty 5

    bl_q = _finalized_line(sup, bulk, admin, qty="10")
    lot_q = create_stock_lot(bl_q, loc, Decimal("5"))
    receive_stock_lot(lot_q, by=admin)
    lot_q.status = StockLot.Status.QUARANTINE  # арранж: карантин
    lot_q.save(update_fields=["status"])
    recompute_balance_row(bl_q, loc)

    return {
        "admin": admin, "loc": loc,
        "item_a": item_a, "il_a": il_a, "item_q": item_q, "il_q": il_q,
        "item_recv": item_recv, "item_sold": item_sold,
        "lot": lot, "bl": bl, "lot_q": lot_q, "bl_q": bl_q,
    }


# --- Создание / проведение ----------------------------------------------------


def test_create_draft_write_off(data):
    doc = create_write_off(reason=R.DEFECT, by=data["admin"])
    assert doc.status == WriteOffDocument.Status.DRAFT
    assert doc.number.startswith("WO-")


def test_create_requires_valid_reason(data):
    with pytest.raises(WriteOffError):
        create_write_off(reason="not-a-reason", by=data["admin"])


def test_cannot_complete_empty(data):
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    with pytest.raises(WriteOffError):
        complete_write_off(doc, by=data["admin"])
    doc.refresh_from_db()
    assert doc.status == WriteOffDocument.Status.DRAFT


# --- Списание PartItem --------------------------------------------------------


def test_write_off_available_part_item(data):
    doc = create_write_off(reason=R.DAMAGED, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    data["item_a"].refresh_from_db()
    doc.refresh_from_db()
    assert data["item_a"].status == PartItem.Status.WRITTEN_OFF
    assert doc.status == WriteOffDocument.Status.COMPLETED
    assert doc.completed_at is not None


def test_write_off_quarantine_part_item(data):
    doc = create_write_off(reason=R.DISPOSAL, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_q"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    data["item_q"].refresh_from_db()
    assert data["item_q"].status == PartItem.Status.WRITTEN_OFF


def test_write_off_creates_movement_item(data):
    doc = create_write_off(reason=R.DAMAGED, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    mv = StockMovement.objects.get(
        movement_type=StockMovement.MovementType.WRITE_OFF_ITEM, part_item=data["item_a"]
    )
    assert mv.from_location_id == data["loc"].pk
    assert mv.to_location_id is None
    assert mv.quantity == Decimal("1")
    assert mv.document_type == "write_off"
    assert mv.document_id == doc.pk


def test_write_off_decreases_balance(data):
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    # Единственный экземпляр этой строки списан → строка кэша удалена.
    assert not StockBalance.objects.filter(batch_line=data["il_a"], location=data["loc"]).exists()


def test_write_off_line_freezes_cost(data):
    doc = create_write_off(reason=R.DEFECT, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    line = doc.lines.get()
    assert line.unit_cost_rub == Decimal("120.00")
    assert line.total_cost_rub == Decimal("120.00")
    assert line.written_off_at is not None
    doc.refresh_from_db()
    assert doc.cost_total == Decimal("120.00")


# --- Списание StockLot --------------------------------------------------------


def test_write_off_stock_lot_quantity(data):
    doc = create_write_off(reason=R.DAMAGED, by=data["admin"])
    add_stock_lot_to_write_off(doc, data["lot"], Decimal("2"), by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("3")
    assert data["lot"].status == StockLot.Status.AVAILABLE  # частичное — статус не меняется
    line = doc.lines.get()
    assert line.unit_cost_rub == Decimal("104.00")
    assert line.total_cost_rub == Decimal("208.00")


def test_write_off_quarantine_stock_lot(data):
    doc = create_write_off(reason=R.OBSOLETE, by=data["admin"])
    add_stock_lot_to_write_off(doc, data["lot_q"], Decimal("2"), by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    data["lot_q"].refresh_from_db()
    assert data["lot_q"].quantity == Decimal("3")
    assert data["lot_q"].status == StockLot.Status.QUARANTINE


def test_fully_written_off_lot_is_written_off_not_depleted(data):
    doc = create_write_off(reason=R.DISPOSAL, by=data["admin"])
    add_stock_lot_to_write_off(doc, data["lot"], Decimal("5"), by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("0")
    assert data["lot"].status == StockLot.Status.WRITTEN_OFF  # НЕ depleted
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.WRITE_OFF_LOT, stock_lot=data["lot"]
    ).exists()


def test_cannot_write_off_lot_over_available(data):
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    with pytest.raises(WriteOffError):
        add_stock_lot_to_write_off(doc, data["lot"], Decimal("6"), by=data["admin"])


def test_cannot_write_off_lot_zero_quantity(data):
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    with pytest.raises(WriteOffError):
        add_stock_lot_to_write_off(doc, data["lot"], Decimal("0"), by=data["admin"])


# --- Инварианты статуса / резерва --------------------------------------------


def test_cannot_write_off_receiving(data):
    doc = create_write_off(reason=R.DEFECT, by=data["admin"])
    with pytest.raises(WriteOffError):
        add_part_item_to_write_off(doc, data["item_recv"], by=data["admin"])


def test_cannot_write_off_sold(data):
    doc = create_write_off(reason=R.DEFECT, by=data["admin"])
    with pytest.raises(WriteOffError):
        add_part_item_to_write_off(doc, data["item_sold"], by=data["admin"])


def test_cannot_write_off_reserved_item(data):
    r = create_reservation(customer_name="Бронь", by=data["admin"])
    add_part_item_to_reservation(r, data["item_a"], by=data["admin"])
    activate_reservation(r, by=data["admin"])
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    with pytest.raises(WriteOffError):
        add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])


def test_cannot_write_off_reserved_lot_quantity(data):
    r = create_reservation(customer_name="Бронь", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("4"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    # Доступно к списанию 5 − 4 (резерв) = 1; запрос 2 → ошибка.
    with pytest.raises(WriteOffError):
        add_stock_lot_to_write_off(doc, data["lot"], Decimal("2"), by=data["admin"])


def test_cannot_write_off_already_written_off(data):
    doc = create_write_off(reason=R.DAMAGED, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])  # item_a → written_off
    doc2 = create_write_off(reason=R.DAMAGED, by=data["admin"])
    with pytest.raises(WriteOffError):
        add_part_item_to_write_off(doc2, data["item_a"], by=data["admin"])


def test_completed_document_is_immutable(data):
    doc = create_write_off(reason=R.DEFECT, by=data["admin"])
    line = add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    with pytest.raises(WriteOffError):
        complete_write_off(doc, by=data["admin"])
    with pytest.raises(WriteOffError):
        remove_write_off_line(line, by=data["admin"])
    with pytest.raises(WriteOffError):
        cancel_write_off(doc, by=data["admin"])


# --- Границы: списание — не продажа / ремонт / возврат / оплата ---------------


def test_write_off_does_not_create_other_documents(data):
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    assert Sale.objects.count() == 0
    assert RepairOrder.objects.count() == 0
    assert StockReturn.objects.count() == 0


def test_write_off_is_not_payment(data):
    field_names = {f.name for f in WriteOffDocument._meta.get_fields()}
    forbidden = {
        "paid", "payment", "payment_method", "receipt", "cash", "card", "refund",
        "price", "revenue", "profit", "sale",
    }
    assert field_names.isdisjoint(forbidden)


# --- Права / себестоимость ----------------------------------------------------


def test_write_off_list_requires_login(client):
    assert client.get(reverse("write_off_list")).status_code == 302


def test_storekeeper_can_complete(make_user, client, data):
    doc = create_write_off(reason=R.DAMAGED, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("write_off_complete", args=[doc.pk]))
    assert resp.status_code == 302
    doc.refresh_from_db()
    assert doc.status == WriteOffDocument.Status.COMPLETED


def test_seller_cannot_complete(make_user, client, data):
    doc = create_write_off(reason=R.DAMAGED, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("write_off_list")).status_code == 200  # просмотр ок
    assert client.post(reverse("write_off_complete", args=[doc.pk])).status_code == 403
    doc.refresh_from_db()
    assert doc.status == WriteOffDocument.Status.DRAFT


def test_cost_hidden_without_capability(make_user, client, data):
    doc = create_write_off(reason=R.DEFECT, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    make_user("sklad", role=roles.STOREKEEPER)  # имеет manage_write_offs, но не purchase_cost
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("write_off_detail", args=[doc.pk])).content.decode()
    assert "Себестоимость" not in html


def test_cost_visible_for_manager(make_user, client, data):
    doc = create_write_off(reason=R.DEFECT, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    complete_write_off(doc, by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("write_off_detail", args=[doc.pk])).content.decode()
    assert "Себестоимость" in html


# --- Архитектура: view не пишет ledger ---------------------------------------


def test_view_delegates_to_service_without_writing_ledger(make_user, client, data):
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    add_part_item_to_write_off(doc, data["item_a"], by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    m_before = StockMovement.objects.count()
    b_before = sorted(StockBalance.objects.values_list("id", "quantity_physical"))
    with patch("apps.writeoffs.views.complete_write_off") as mock_complete:
        client.post(reverse("write_off_complete", args=[doc.pk]))
    mock_complete.assert_called_once()
    assert StockMovement.objects.count() == m_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_physical")) == b_before


def test_untrusted_params_rechecked(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Несуществующий документ → 404.
    assert client.post(reverse("write_off_complete", args=[999999])).status_code == 404
    # Подмена лота на несуществующий id в add-lot → ошибка формы, без эффекта.
    doc = create_write_off(reason=R.LOST, by=data["admin"])
    resp = client.post(
        reverse("write_off_add_lot", args=[doc.pk]),
        {"lot": 999999, "quantity": "1"},
    )
    assert resp.status_code == 302
    assert not WriteOffLine.objects.filter(write_off=doc).exists()
