"""Слой 15 — резервы (коммерческая бронь).

Покрывает план 15-layer-15-reservations.md §17. Ключевое: резерв уменьшает
доступность, но НЕ меняет физический остаток — `StockMovement` не создаётся,
`StockLot.quantity` не уменьшается, а `StockBalance.quantity_reserved` — кэш
поверх активных `ReservationLine`.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import PartItem, StockBalance, StockMovement
from apps.inventory.services import (
    check_stock_balance,
    create_part_items,
    create_stock_lot,
    rebuild_stock_balance,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Reservation, ReservationLine
from apps.sales.services import (
    ReservationError,
    activate_reservation,
    add_part_item_to_reservation,
    add_stock_lot_to_reservation,
    cancel_reservation,
    create_reservation,
    expire_reservations,
    reserved_for,
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
        name="Насос-Резерв", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("500"), min_price=Decimal("400"),
    )
    iline = _finalized_line(sup, serial, admin, qty="2")
    item = create_part_items(iline, 1, serial_number="SN-RES-1")[0]
    receive_part_item(item, to_location=loc, by=admin)  # available @ loc
    item_receiving = create_part_items(iline, 1, serial_number="SN-RES-2")[0]  # receiving

    bulk = PartType.objects.create(
        name="Болт-Резерв", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    bline = _finalized_line(sup, bulk, admin, qty="10")
    lot = create_stock_lot(bline, loc, Decimal("5"))
    receive_stock_lot(lot, by=admin)  # available @ loc, qty 5

    return {
        "admin": admin, "serial": serial, "item": item, "item_receiving": item_receiving,
        "bulk": bulk, "lot": lot, "loc": loc,
    }


def _balance(obj, loc):
    return StockBalance.objects.get(batch_line=obj.batch_line, location=loc)


# --- Создание / активация ----------------------------------------------------


def test_create_reservation_draft(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    assert r.status == Reservation.Status.DRAFT
    assert r.number.startswith("РЕЗ-")


def test_create_reservation_requires_customer(data):
    with pytest.raises(ReservationError):
        create_reservation(customer_name="  ", by=data["admin"])


def test_cannot_activate_empty_reservation(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    with pytest.raises(ReservationError):
        activate_reservation(r, by=data["admin"])
    r.refresh_from_db()
    assert r.status == Reservation.Status.DRAFT


# --- Резерв PartItem ---------------------------------------------------------


def test_reserve_part_item_holds(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    activate_reservation(r, by=data["admin"])
    bal = _balance(data["item"], data["loc"])
    assert bal.quantity_physical == Decimal("1")
    assert bal.quantity_reserved == Decimal("1")
    assert bal.quantity_available == Decimal("0")


def test_cannot_reserve_part_item_twice(data):
    r1 = create_reservation(customer_name="Иван", by=data["admin"])
    add_part_item_to_reservation(r1, data["item"], by=data["admin"])
    activate_reservation(r1, by=data["admin"])
    r2 = create_reservation(customer_name="Пётр", by=data["admin"])
    with pytest.raises(ReservationError):
        add_part_item_to_reservation(r2, data["item"], by=data["admin"])


def test_cannot_reserve_receiving_part_item(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    with pytest.raises(ReservationError):
        add_part_item_to_reservation(r, data["item_receiving"], by=data["admin"])


# --- Резерв StockLot ---------------------------------------------------------


def test_reserve_lot_quantity_partial(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("2"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    bal = _balance(data["lot"], data["loc"])
    assert bal.quantity_physical == Decimal("5")
    assert bal.quantity_reserved == Decimal("2")
    assert bal.quantity_available == Decimal("3")
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == Decimal("5")  # физический остаток не тронут


def test_cannot_reserve_lot_over_available(data):
    r1 = create_reservation(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_reservation(r1, data["lot"], Decimal("4"), by=data["admin"])
    activate_reservation(r1, by=data["admin"])
    r2 = create_reservation(customer_name="Пётр", by=data["admin"])
    with pytest.raises(ReservationError):
        add_stock_lot_to_reservation(r2, data["lot"], Decimal("2"), by=data["admin"])


def test_cannot_overcommit_within_one_draft(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("3"), by=data["admin"])
    with pytest.raises(ReservationError):
        add_stock_lot_to_reservation(r, data["lot"], Decimal("3"), by=data["admin"])


# --- Отмена освобождает ------------------------------------------------------


def test_cancel_frees_quantity(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("2"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    cancel_reservation(r, by=data["admin"])
    bal = _balance(data["lot"], data["loc"])
    assert bal.quantity_reserved == Decimal("0")
    assert bal.quantity_available == Decimal("5")
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELED
    assert r.canceled_at is not None


# --- Границы: нет движения, физический остаток не меняется --------------------


def test_reservation_creates_no_movement(data):
    before = StockMovement.objects.count()
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("2"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    cancel_reservation(r, by=data["admin"])
    assert StockMovement.objects.count() == before


def test_reservation_does_not_change_physical(data):
    items_before = PartItem.objects.filter(status=PartItem.Status.AVAILABLE).count()
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("2"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    data["item"].refresh_from_db()
    data["lot"].refresh_from_db()
    assert data["item"].status == PartItem.Status.AVAILABLE  # статус не reserved
    assert data["lot"].quantity == Decimal("5")
    assert PartItem.objects.filter(status=PartItem.Status.AVAILABLE).count() == items_before


# --- Кэш: пересборка и сверка ------------------------------------------------


def test_rebuild_and_check_consistent(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("2"), by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    activate_reservation(r, by=data["admin"])
    rebuild_stock_balance()  # пересобирает reserved через хук
    assert check_stock_balance() == []
    bal = _balance(data["lot"], data["loc"])
    assert bal.quantity_reserved == Decimal("2")


# --- Срок резерва ------------------------------------------------------------


def test_expired_not_counted_and_command(data):
    past = timezone.now() - timedelta(hours=1)
    r = create_reservation(customer_name="Иван", expires_at=past, by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("2"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    # Просрочка не держит доступность сразу (провайдер фильтрует по expires_at).
    assert reserved_for(data["lot"].batch_line, data["loc"]) == Decimal("0")
    # Команда нормализует статус и пересобирает кэш.
    n = expire_reservations()
    assert n == 1
    r.refresh_from_db()
    assert r.status == Reservation.Status.EXPIRED
    bal = _balance(data["lot"], data["loc"])
    assert bal.quantity_reserved == Decimal("0")
    assert bal.quantity_available == Decimal("5")


# --- Инварианты БД -----------------------------------------------------------


def test_line_requires_item_xor_lot(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ReservationLine.objects.create(
                reservation=r, part_type=data["serial"], quantity=Decimal("1")
            )


def test_line_quantity_must_be_positive(data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ReservationLine.objects.create(
                reservation=r, part_type=data["bulk"],
                stock_lot=data["lot"], quantity=Decimal("0"),
            )


# --- Права / UI --------------------------------------------------------------


def test_reservation_list_requires_login(client):
    assert client.get(reverse("reservation_list")).status_code == 302


def test_seller_can_create_reservation(make_user, client, data):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    resp = client.post(
        reverse("reservation_create"),
        {"customer_name": "Клиент", "customer_phone": "", "comment": ""},
    )
    assert resp.status_code == 302
    assert Reservation.objects.filter(customer_name="Клиент").exists()


def test_storekeeper_cannot_create_but_can_view(make_user, client, data):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    assert client.get(reverse("reservation_list")).status_code == 200  # просмотр ок
    resp = client.post(reverse("reservation_create"), {"customer_name": "X"})
    assert resp.status_code == 403  # создание запрещено


def test_seller_cannot_see_costs_in_detail(make_user, client, data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    html = client.get(reverse("reservation_detail", args=[r.pk])).content.decode()
    assert "Себестоимость" not in html


def test_manager_sees_costs_in_detail(make_user, client, data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("reservation_detail", args=[r.pk])).content.decode()
    assert "Себестоимость" in html


def test_search_shows_reserved(make_user, client, data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_stock_lot_to_reservation(r, data["lot"], Decimal("2"), by=data["admin"])
    activate_reservation(r, by=data["admin"])
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    html = client.get(reverse("part_search"), {"q": "Болт-Резерв"}).content.decode()
    assert "зарезервировано" in html


# --- Архитектура: view не пишет ledger напрямую ------------------------------


def test_view_delegates_to_service_without_writing_ledger(make_user, client, data):
    r = create_reservation(customer_name="Иван", by=data["admin"])
    add_part_item_to_reservation(r, data["item"], by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    m_before = StockMovement.objects.count()
    b_before = sorted(StockBalance.objects.values_list("id", "quantity_reserved"))
    with patch("apps.sales.views.activate_reservation") as mock_activate:
        client.post(reverse("reservation_activate", args=[r.pk]))
    mock_activate.assert_called_once()
    # При замоканном сервисе вьюха сама ничего в ledger не пишет.
    assert StockMovement.objects.count() == m_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_reserved")) == b_before


def test_untrusted_params_rechecked(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Несуществующий резерв → 404 (объект перечитывается из БД).
    assert client.post(reverse("reservation_activate", args=[999999])).status_code == 404
    # Подмена лота на несуществующий id в форме add-lot → ошибка, без эффекта.
    r = create_reservation(customer_name="Иван", by=data["admin"])
    resp = client.post(
        reverse("reservation_add_lot", args=[r.pk]),
        {"lot": 999999, "quantity": "1"},
    )
    assert resp.status_code == 302
    assert not r.lines.exists()
