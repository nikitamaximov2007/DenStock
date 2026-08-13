"""Корзина сканера: одна продажа / один ремонт на много отсканированных позиций.

Ключевые гарантии, которые здесь проверяются:

* скан НЕ трогает склад — остаток, движения и статусы лотов до проведения
  не меняются вообще;
* повторный скан той же canonical детали увеличивает количество существующей
  позиции, а не добавляет вторую строку, даже если сканировали разные номера
  (OEM, штрихкод) — личность позиции определяет деталь, а не строка сканера;
* разные детали остаются разными позициями;
* количество можно поправить руками, позицию убрать, корзину очистить (через
  подтверждение), и всё это остатки не меняет;
* проведение создаёт ОДИН документ со всеми строками и меняет склад ровно один
  раз; больше доступного провести нельзя;
* журнал действий остаётся построчным (снимок номера/ячейки на позицию), но все
  записи ссылаются на один документ, и сторно отменяет их все.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.cart import (
    KIND_REPAIR,
    KIND_SALE,
    add_scan,
    cart_rows,
    clear_cart,
    complete_cart,
    open_cart,
    remove_row,
    set_row_quantity,
)
from apps.actions.models import WarehouseAction
from apps.actions.services import ActionError, cancel_warehouse_action
from apps.catalog.models import Category, PartBarcode, PartNumber, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale
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


def _finalized_line(sup, part, admin, *, qty, unit_cost="100"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(qty), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


def _stock(part, location, qty, sup, admin):
    line = _finalized_line(sup, part, admin, qty=str(qty))
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    loc1 = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D03-C08", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка 2", code="S04-D01-C01", storage_allowed=True, is_active=True
    )
    bolt = PartType.objects.create(
        name="Болт", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=bolt, value="700100", kind=PartNumber.Kind.OEM)
    PartBarcode.objects.create(part=bolt, value="BAR-700100")
    ring = PartType.objects.create(
        name="Кольцо", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("250"),
    )
    PartNumber.objects.create(part=ring, value="700200", kind=PartNumber.Kind.OEM)
    return {
        "sup": sup, "cat": cat, "unit": unit, "admin": admin,
        "loc1": loc1, "loc2": loc2, "bolt": bolt, "ring": ring,
        "bolt_lot": _stock(bolt, loc1, 10, sup, admin),
        "ring_lot": _stock(ring, loc1, 4, sup, admin),
        "bolt_lot2": _stock(bolt, loc2, 3, sup, admin),
    }


def _login(client, make_user, *, role=None, superuser=True, name="boss"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


# --- Скан ничего не списывает ------------------------------------------------------------


def test_scan_into_cart_does_not_touch_stock(data):
    movements_before = StockMovement.objects.count()
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])
    add_scan(cart, data["ring"], data["loc1"], by=data["admin"])

    data["bolt_lot"].refresh_from_db()
    data["ring_lot"].refresh_from_db()
    assert data["bolt_lot"].quantity == Decimal("10")
    assert data["ring_lot"].quantity == Decimal("4")
    assert data["bolt_lot"].status == StockLot.Status.AVAILABLE
    assert StockMovement.objects.count() == movements_before
    cart.refresh_from_db()
    assert cart.status == Sale.Status.DRAFT


# --- Личность позиции: canonical деталь --------------------------------------------------


def test_repeated_scan_of_same_part_increments_one_row(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])

    rows = cart_rows(cart)
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("3")
    assert rows[0].part == data["bolt"]


def test_different_parts_are_separate_rows(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])
    add_scan(cart, data["ring"], data["loc1"], by=data["admin"])

    rows = sorted(cart_rows(cart), key=lambda row: row.part.name)
    assert [row.part.name for row in rows] == ["Болт", "Кольцо"]
    assert all(row.quantity == Decimal("1") for row in rows)


def test_same_part_in_another_cell_is_its_own_row(data):
    """Разные ячейки — разные источники остатка, поэтому и позиции разные."""
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])
    add_scan(cart, data["bolt"], data["loc2"], by=data["admin"])

    rows = sorted(cart_rows(cart), key=lambda row: row.location.code)
    assert [row.location.code for row in rows] == ["S01-D03-C08", "S04-D01-C01"]


def test_scan_by_oem_and_by_barcode_is_one_row(client, make_user, data):
    """Сканировали разные строки (номер и штрихкод) — деталь одна, строка одна."""
    _login(client, make_user)
    for scanned in ("700100", "BAR-700100"):
        resp = client.post(
            reverse("actions_cart_add"),
            {
                "part_id": data["bolt"].pk, "location_id": data["loc1"].pk,
                "action_type": "sale", "quantity": "1", "q": scanned,
            },
            follow=True,
        )
        assert resp.status_code == 200
    cart = Sale.objects.get(status=Sale.Status.DRAFT)
    rows = cart_rows(cart)
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("2")


# --- Ручное редактирование ----------------------------------------------------------------


def test_manual_quantity_edit_rebuilds_row(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])
    set_row_quantity(cart, data["bolt"], data["loc1"], "4", by=data["admin"])

    rows = cart_rows(cart)
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("4")
    data["bolt_lot"].refresh_from_db()
    assert data["bolt_lot"].quantity == Decimal("10")  # склад не тронут


def test_zero_quantity_removes_row(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])
    assert set_row_quantity(cart, data["bolt"], data["loc1"], "0", by=data["admin"]) is None
    assert cart_rows(cart) == []


def test_remove_and_clear_do_not_touch_stock(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    add_scan(cart, data["ring"], data["loc1"], by=data["admin"])
    movements_before = StockMovement.objects.count()

    remove_row(cart, data["ring"], data["loc1"], by=data["admin"])
    assert [row.part for row in cart_rows(cart)] == [data["bolt"]]
    clear_cart(cart, by=data["admin"])
    assert cart_rows(cart) == []

    data["bolt_lot"].refresh_from_db()
    assert data["bolt_lot"].quantity == Decimal("10")
    assert StockMovement.objects.count() == movements_before


def test_failed_first_scan_leaves_no_empty_draft(client, make_user, data):
    _login(client, make_user)
    resp = client.post(
        reverse("actions_cart_add"),
        {
            "part_id": data["ring"].pk, "location_id": data["loc1"].pk,
            "action_type": "sale", "quantity": "99", "q": "700200",
        },
        follow=True,
    )
    assert resp.status_code == 200
    assert not Sale.objects.exists()


def test_scan_page_shows_cart_panel(client, make_user, data):
    _login(client, make_user)
    client.post(
        reverse("actions_cart_add"),
        {
            "part_id": data["bolt"].pk, "location_id": data["loc1"].pk,
            "action_type": "sale", "quantity": "2", "q": "700100",
        },
    )
    client.post(
        reverse("actions_cart_add"),
        {
            "part_id": data["ring"].pk, "location_id": data["loc1"].pk,
            "action_type": "repair", "quantity": "1", "q": "700200",
        },
    )
    html = client.get(reverse("actions_scan")).content.decode()
    assert "Корзина · Продажа" in html
    assert "Корзина · Выдача в ремонт" in html
    assert "700100" in html  # exact-артикул позиции
    assert "S01-D03-C08" in html
    assert "Склад изменится только при проведении." in html


def test_completed_cart_snapshot_keeps_scanned_number(client, make_user, data):
    """Снимок номера в отчёте такой же точный, как при одиночном проведении."""
    _login(client, make_user)
    client.post(
        reverse("actions_cart_add"),
        {
            "part_id": data["bolt"].pk, "location_id": data["loc1"].pk,
            "action_type": "sale", "quantity": "2", "q": "700100",
        },
    )
    client.post(
        reverse("actions_cart_complete"),
        {"kind": KIND_SALE, "customer_comment": "Иванов", "q": "700100"},
        follow=True,
    )
    action = WarehouseAction.objects.get(part_type=data["bolt"])
    assert action.part_number == "700100"
    assert action.quantity == Decimal("2")


def test_clear_requires_confirmation_page(client, make_user, data):
    _login(client, make_user)
    client.post(
        reverse("actions_cart_add"),
        {
            "part_id": data["bolt"].pk, "location_id": data["loc1"].pk,
            "action_type": "sale", "quantity": "2", "q": "700100",
        },
    )
    page = client.get(reverse("actions_cart_clear", args=[KIND_SALE]))
    assert page.status_code == 200
    assert "Очистить корзину" in page.content.decode()
    assert Sale.objects.filter(status=Sale.Status.DRAFT).exists()  # GET ничего не удалил

    client.post(reverse("actions_cart_clear", args=[KIND_SALE]), follow=True)
    assert not Sale.objects.filter(status=Sale.Status.DRAFT).exists()
    data["bolt_lot"].refresh_from_db()
    assert data["bolt_lot"].quantity == Decimal("10")


# --- Нельзя набрать больше доступного -----------------------------------------------------


def test_cart_cannot_exceed_available(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    with pytest.raises(ActionError):
        add_scan(cart, data["ring"], data["loc1"], quantity=Decimal("5"), by=data["admin"])
    assert cart_rows(cart) == []


def test_failed_edit_keeps_previous_quantity(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["ring"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    with pytest.raises(ActionError):
        set_row_quantity(cart, data["ring"], data["loc1"], "99", by=data["admin"])
    cart.refresh_from_db()
    rows = cart_rows(cart)
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("2")


# --- Проведение: один документ на много позиций -------------------------------------------


def test_complete_sale_cart_creates_one_sale_with_many_lines(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    add_scan(cart, data["ring"], data["loc1"], by=data["admin"])
    sales_before = Sale.objects.count()

    actions = complete_cart(cart, customer_comment="Иванов", by=data["admin"])

    assert Sale.objects.count() == sales_before  # новый документ не создавался
    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED
    assert cart.customer_name == "Иванов"
    assert cart.lines.count() == 2
    data["bolt_lot"].refresh_from_db()
    data["ring_lot"].refresh_from_db()
    assert data["bolt_lot"].quantity == Decimal("8")
    assert data["ring_lot"].quantity == Decimal("3")
    # Журнал построчный, документ один.
    assert len(actions) == 2
    assert {action.sale_id for action in actions} == {cart.pk}
    assert all(action.action_type == WarehouseAction.Type.SALE for action in actions)


def test_complete_repair_cart_creates_one_order_with_many_lines(data):
    cart = open_cart(KIND_REPAIR, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("3"), by=data["admin"])
    add_scan(cart, data["ring"], data["loc1"], by=data["admin"])

    actions = complete_cart(cart, customer_comment="Петров, квадроцикл", by=data["admin"])

    cart.refresh_from_db()
    assert cart.status == RepairOrder.Status.COMPLETED
    assert cart.lines.count() == 2
    assert cart.cost_total > 0
    data["bolt_lot"].refresh_from_db()
    assert data["bolt_lot"].quantity == Decimal("7")
    assert len(actions) == 2
    assert {action.repair_order_id for action in actions} == {cart.pk}
    assert all(action.action_type == WarehouseAction.Type.REPAIR for action in actions)


def test_completed_actions_keep_identity_snapshots(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    actions = complete_cart(
        cart,
        customer_comment="Иванов",
        by=data["admin"],
        scanned_numbers={f"{data['bolt'].pk}:{data['loc1'].pk}": "700100"},
    )
    action = actions[0]
    assert action.part_number == "700100"
    assert action.part_name == "Болт"
    assert action.location_code == "S01-D03-C08"
    assert action.quantity == Decimal("2")
    assert action.total_price_rub == Decimal("200.00")


def test_empty_cart_cannot_be_completed(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    with pytest.raises(ActionError):
        complete_cart(cart, customer_comment="Иванов", by=data["admin"])


def test_completion_requires_customer(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], by=data["admin"])
    with pytest.raises(ActionError):
        complete_cart(cart, customer_comment="   ", by=data["admin"])
    cart.refresh_from_db()
    assert cart.status == Sale.Status.DRAFT


def test_double_submit_is_idempotent(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    first = complete_cart(
        cart, customer_comment="Иванов", by=data["admin"], request_token="token-abc"
    )
    second = complete_cart(
        cart, customer_comment="Иванов", by=data["admin"], request_token="token-abc"
    )
    assert [a.pk for a in first] == [a.pk for a in second]
    data["bolt_lot"].refresh_from_db()
    assert data["bolt_lot"].quantity == Decimal("8")  # списано один раз


# --- Оверселла нет: побеждает тот, кто провёл первым ---------------------------------------


def test_second_cart_cannot_oversell_after_first_completed(data):
    first = open_cart(KIND_SALE, by=data["admin"])
    second = open_cart(KIND_SALE, by=data["admin"])
    add_scan(first, data["ring"], data["loc1"], quantity=Decimal("4"), by=data["admin"])
    add_scan(second, data["ring"], data["loc1"], quantity=Decimal("4"), by=data["admin"])

    complete_cart(first, customer_comment="Первый", by=data["admin"])
    with pytest.raises(ActionError):
        complete_cart(second, customer_comment="Второй", by=data["admin"])

    data["ring_lot"].refresh_from_db()
    assert data["ring_lot"].quantity == Decimal("0")  # в минус не ушли
    second.refresh_from_db()
    assert second.status == Sale.Status.DRAFT


# --- Сторно мультипозиционной продажи ------------------------------------------------------


def test_cancel_multi_item_sale_returns_each_line_to_its_cell(data):
    cart = open_cart(KIND_SALE, by=data["admin"])
    add_scan(cart, data["bolt"], data["loc1"], quantity=Decimal("2"), by=data["admin"])
    add_scan(cart, data["bolt"], data["loc2"], quantity=Decimal("1"), by=data["admin"])
    actions = complete_cart(cart, customer_comment="Иванов", by=data["admin"])

    cancel_warehouse_action(actions[0], by=data["admin"], reason="ошибка кассира")

    # Обе записи журнала отменены — «висящих» активных строк не осталось.
    for action in actions:
        action.refresh_from_db()
        assert action.status == WarehouseAction.Status.CANCELLED
    cart.refresh_from_db()
    assert cart.status == Sale.Status.VOIDED
    # Каждая строка вернулась в СВОЮ ячейку.
    for location, expected in ((data["loc1"], Decimal("10")), (data["loc2"], Decimal("3"))):
        returned = sum(
            lot.quantity
            for lot in StockLot.objects.filter(
                part_type=data["bolt"], location=location, status=StockLot.Status.AVAILABLE
            )
        )
        assert returned == expected


# --- Права и границы -----------------------------------------------------------------------


def test_reserve_cannot_be_added_to_cart(client, make_user, data):
    _login(client, make_user)
    resp = client.post(
        reverse("actions_cart_add"),
        {
            "part_id": data["bolt"].pk, "location_id": data["loc1"].pk,
            "action_type": "reserve", "quantity": "1", "q": "700100",
        },
        follow=True,
    )
    assert "Корзина доступна для продажи и выдачи в ремонт." in resp.content.decode()
    assert not Sale.objects.exists()


def test_cart_add_respects_action_permission(client, make_user, data):
    from apps.accounts import roles

    _login(client, make_user, role=roles.STOREKEEPER, superuser=False, name="sklad")
    resp = client.post(
        reverse("actions_cart_add"),
        {
            "part_id": data["bolt"].pk, "location_id": data["loc1"].pk,
            "action_type": "sale", "quantity": "1", "q": "700100",
        },
    )
    assert resp.status_code == 403
