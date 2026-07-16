"""Layer 28 — «Поступление»: документ прихода поставки.

Ключевые гарантии: черновик НЕ трогает склад (ни партий, ни движений, ни
остатков); проведение атомарно создаёт партию + себестоимость + остатки через
СУЩЕСТВУЮЩИЕ сервисы; повторное проведение и правка проведённого запрещены;
поштучные и количественные детали принимаются согласно их режиму учёта;
«+ Новая деталь» возвращает в черновик и не создаёт остатков.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db.models import Sum
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.procurement.models import Batch
from apps.receipts.models import Receipt, ReceiptLine
from apps.receipts.services import (
    ReceiptError,
    add_line,
    create_receipt,
    post_receipt,
    receipt_totals,
    remove_line,
    update_line,
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


@pytest.fixture
def refs(db):
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    sup = Supplier.objects.create(name="ООО Поставка")
    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    serial = PartType.objects.create(
        name="Насос-Приход", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
    )
    bulk = PartType.objects.create(
        name="Болт-Приход", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    return {"cat": cat, "unit": unit, "sup": sup, "loc": loc, "serial": serial, "bulk": bulk}


def _stock_snapshot():
    return {
        "movements": StockMovement.objects.count(),
        "batches": Batch.objects.count(),
        "lots": StockLot.objects.count(),
        "items": PartItem.objects.count(),
        "balances": sorted(StockBalance.objects.values_list("id", "quantity_available")),
    }


def _draft(refs, admin, *, with_lines=True):
    receipt = create_receipt(supplier=refs["sup"], comment="тест", by=admin)
    if with_lines:
        add_line(
            receipt, part_type=refs["serial"], quantity=Decimal("2"),
            unit_cost_rub=Decimal("100"), location=refs["loc"],
        )
        add_line(
            receipt, part_type=refs["bulk"], quantity=Decimal("5"),
            unit_cost_rub=Decimal("10"), location=refs["loc"],
        )
    return receipt


# --- Черновик: не трогает склад ------------------------------------------------


def test_draft_does_not_touch_stock(refs, admin):
    before = _stock_snapshot()
    receipt = _draft(refs, admin)
    assert receipt.status == Receipt.Status.DRAFT
    assert receipt.number.startswith("ПОС-")
    assert _stock_snapshot() == before  # ни партий, ни движений, ни остатков


def test_draft_edit_lines(refs, admin):
    receipt = _draft(refs, admin, with_lines=False)
    line = add_line(
        receipt, part_type=refs["bulk"], quantity=Decimal("3"),
        unit_cost_rub=Decimal("7"), location=refs["loc"],
    )
    update_line(
        line, part_type=refs["bulk"], quantity=Decimal("4"),
        unit_cost_rub=Decimal("8"), location=refs["loc"], comment="уточнено",
    )
    line.refresh_from_db()
    assert line.quantity == Decimal("4")
    assert line.unit_cost_rub == Decimal("8.00")
    remove_line(line)
    assert receipt.lines.count() == 0


def test_totals(refs, admin):
    receipt = _draft(refs, admin)
    totals = receipt_totals(receipt)
    assert totals["lines"] == 2
    assert totals["quantity"] == Decimal("7")
    assert totals["cost"] == Decimal("250.00")  # 2*100 + 5*10


# --- Проведение -----------------------------------------------------------------


def test_post_creates_stock_via_existing_services(refs, admin):
    receipt = post_receipt(_draft(refs, admin), by=admin)
    assert receipt.status == Receipt.Status.POSTED
    assert receipt.posted_by == admin and receipt.posted_at is not None

    # Партия создана и себестоимость зафиксирована.
    batch = receipt.batch
    assert batch is not None and batch.cost_finalized is True
    assert batch.lines.count() == 2

    # Поштучная деталь: 2 экземпляра available в ячейке, landed cost = цене.
    items = PartItem.objects.filter(batch=batch)
    assert items.count() == 2
    for item in items:
        assert item.status == PartItem.Status.AVAILABLE
        assert item.current_location == refs["loc"]
        assert item.landed_cost_rub == Decimal("100.00")

    # Количественная: лот 5 шт available.
    lot = StockLot.objects.get(batch=batch)
    assert lot.status == StockLot.Status.AVAILABLE
    assert lot.quantity == Decimal("5")
    assert lot.landed_unit_cost_rub == Decimal("10.00")

    # Движения прихода: 2 x receive_item + 1 x receive_lot.
    moves = StockMovement.objects.filter(batch=batch)
    assert moves.filter(movement_type=StockMovement.MovementType.RECEIVE_ITEM).count() == 2
    assert moves.filter(movement_type=StockMovement.MovementType.RECEIVE_LOT).count() == 1

    # Остатки: по serial 2 доступно, по bulk 5 доступно.
    serial_avail = StockBalance.objects.filter(part_type=refs["serial"]).aggregate(
        s=Sum("quantity_available")
    )["s"]
    assert serial_avail == Decimal("2")
    bulk_bal = StockBalance.objects.get(part_type=refs["bulk"])
    assert bulk_bal.quantity_available == Decimal("5")

    # Ссылки на строки партии проставлены.
    assert all(line.batch_line_id for line in receipt.lines.all())


def test_cannot_post_empty(refs, admin):
    receipt = _draft(refs, admin, with_lines=False)
    with pytest.raises(ReceiptError):
        post_receipt(receipt, by=admin)


def test_cannot_post_without_supplier(refs, admin):
    receipt = create_receipt(supplier=None, by=admin)
    add_line(
        receipt, part_type=refs["bulk"], quantity=Decimal("1"),
        unit_cost_rub=Decimal("1"), location=refs["loc"],
    )
    with pytest.raises(ReceiptError):
        post_receipt(receipt, by=admin)


def test_cannot_post_twice(refs, admin):
    receipt = post_receipt(_draft(refs, admin), by=admin)
    before = _stock_snapshot()
    with pytest.raises(ReceiptError):
        post_receipt(receipt, by=admin)
    assert _stock_snapshot() == before  # идемпотентно: ничего не задвоилось


def test_invalid_line_rolls_back_everything(refs, admin):
    receipt = _draft(refs, admin)
    # Поштучная деталь с дробным количеством: провести нельзя.
    ReceiptLine.objects.create(
        receipt=receipt, part_type=refs["serial"], quantity=Decimal("1.5"),
        unit_cost_rub=Decimal("1"), location=refs["loc"],
    )
    before = _stock_snapshot()
    with pytest.raises(ReceiptError):
        post_receipt(receipt, by=admin)
    receipt.refresh_from_db()
    assert receipt.status == Receipt.Status.DRAFT
    assert _stock_snapshot() == before  # никакого «половинного» прихода


def test_serial_requires_integer_quantity(refs, admin):
    receipt = _draft(refs, admin, with_lines=False)
    with pytest.raises(ReceiptError):
        add_line(
            receipt, part_type=refs["serial"], quantity=Decimal("2.5"),
            unit_cost_rub=Decimal("1"), location=refs["loc"],
        )


def test_line_validation(refs, admin):
    receipt = _draft(refs, admin, with_lines=False)
    with pytest.raises(ReceiptError):  # количество <= 0
        add_line(
            receipt, part_type=refs["bulk"], quantity=Decimal("0"),
            unit_cost_rub=Decimal("1"), location=refs["loc"],
        )
    with pytest.raises(ReceiptError):  # отрицательная себестоимость
        add_line(
            receipt, part_type=refs["bulk"], quantity=Decimal("1"),
            unit_cost_rub=Decimal("-1"), location=refs["loc"],
        )
    bad_loc = StorageLocation.objects.create(
        name="Витрина", code="V-01", storage_allowed=False, is_active=True
    )
    with pytest.raises(ReceiptError):  # ячейка без хранения
        add_line(
            receipt, part_type=refs["bulk"], quantity=Decimal("1"),
            unit_cost_rub=Decimal("1"), location=bad_loc,
        )


def test_posted_is_readonly(refs, admin):
    receipt = post_receipt(_draft(refs, admin), by=admin)
    line = receipt.lines.first()
    with pytest.raises(ReceiptError):
        add_line(
            receipt, part_type=refs["bulk"], quantity=Decimal("1"),
            unit_cost_rub=Decimal("1"), location=refs["loc"],
        )
    with pytest.raises(ReceiptError):
        update_line(
            line, part_type=line.part_type, quantity=Decimal("9"),
            unit_cost_rub=line.unit_cost_rub, location=line.location,
        )
    with pytest.raises(ReceiptError):
        remove_line(line)


# --- Экраны и права ---------------------------------------------------------------


def _login(client, make_user, *, role=None, superuser=False, name="u"):
    user = make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)
    return user


def test_list_opens(client, make_user, refs):
    _login(client, make_user, superuser=True)
    resp = client.get(reverse("receipt_list"))
    assert resp.status_code == 200
    assert "Поступлений пока нет" in resp.content.decode()


def test_storekeeper_can_work_seller_cannot(client, make_user, refs):
    _login(client, make_user, role=roles.STOREKEEPER, name="sklad")
    assert client.get(reverse("receipt_list")).status_code == 200
    client.logout()
    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.get(reverse("receipt_list")).status_code == 403


def test_create_and_add_line_via_views(client, make_user, refs):
    _login(client, make_user, superuser=True)
    before = _stock_snapshot()
    resp = client.post(
        reverse("receipt_create"),
        {"supplier": refs["sup"].pk, "received_at": "2026-07-04", "comment": "от Дениса"},
    )
    assert resp.status_code == 302
    receipt = Receipt.objects.latest("pk")
    resp = client.post(
        reverse("receipt_add_line", args=[receipt.pk]),
        {
            "part_type": refs["bulk"].pk, "quantity": "5",
            "unit_cost_rub": "10", "location": refs["loc"].pk, "comment": "",
        },
    )
    assert resp.status_code == 302
    assert receipt.lines.count() == 1
    assert _stock_snapshot() == before  # черновик через UI тоже не трогает склад


def test_post_via_view(client, make_user, refs, admin):
    user = _login(client, make_user, superuser=True, name="boss")
    receipt = _draft(refs, admin)
    resp = client.post(reverse("receipt_post", args=[receipt.pk]))
    assert resp.status_code == 302
    receipt.refresh_from_db()
    assert receipt.status == Receipt.Status.POSTED
    assert receipt.posted_by == user
    # Детальная страница проведённого показывает связи.
    html = client.get(reverse("receipt_detail", args=[receipt.pk])).content.decode()
    assert receipt.batch.number in html
    assert "Проведено" in html


def test_seller_cannot_post(client, make_user, refs, admin):
    receipt = _draft(refs, admin)
    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.post(reverse("receipt_post", args=[receipt.pk])).status_code == 403
    receipt.refresh_from_db()
    assert receipt.status == Receipt.Status.DRAFT


def test_posted_edit_views_redirect(client, make_user, refs, admin):
    receipt = post_receipt(_draft(refs, admin), by=admin)
    _login(client, make_user, superuser=True, name="boss")
    resp = client.get(reverse("receipt_edit", args=[receipt.pk]))
    assert resp.status_code == 302  # проведённое: правка шапки недоступна
    line = receipt.lines.first()
    resp = client.get(reverse("receipt_line_edit", args=[line.pk]))
    assert resp.status_code == 302


# --- «+ Новая деталь» из поступления ------------------------------------------------


def test_inline_part_creation_returns_to_receipt(client, make_user, refs, admin):
    receipt = _draft(refs, admin, with_lines=False)
    _login(client, make_user, superuser=True, name="boss")
    detail_url = reverse("receipt_detail", args=[receipt.pk])
    before = _stock_snapshot()
    resp = client.post(
        reverse("part_create") + f"?next={detail_url}",
        {
            "name": "Новая-Из-Поступления", "category": refs["cat"].pk,
            "unit": refs["unit"].pk, "tracking_mode": "bulk", "min_stock_level": "0",
        },
    )
    assert resp.status_code == 302
    part = PartType.objects.get(name="Новая-Из-Поступления")
    assert resp["Location"] == f"{detail_url}?new_part={part.pk}"
    # Деталь появилась в каталоге, но остатков НЕ создала.
    assert _stock_snapshot() == before
    # Черновик цел, форма предлагает новую деталь выбранной.
    html = client.get(resp["Location"]).content.decode()
    assert f'value="{part.pk}" selected' in html


def test_inline_part_creation_rejects_external_next(client, make_user, refs):
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(
        reverse("part_create") + "?next=https://evil.example/",
        {
            "name": "Зло", "category": refs["cat"].pk,
            "unit": refs["unit"].pk, "tracking_mode": "bulk", "min_stock_level": "0",
        },
    )
    assert resp.status_code == 302
    assert "evil.example" not in resp["Location"]  # уходим на part_detail


# --- Навигация ----------------------------------------------------------------------


def test_nav_receipt_is_link_not_stub(client, make_user, refs):
    _login(client, make_user, role=roles.STOREKEEPER, name="sklad")
    html = client.get(reverse("receipt_list")).content.decode()
    assert 'href="/receipts/"' in html
    assert "nav__link--soon" not in html  # заглушек в меню не осталось
    assert "Приёмка сканером" in html


def test_page_has_no_em_dash(client, make_user, refs, admin):
    receipt = post_receipt(_draft(refs, admin), by=admin)
    _login(client, make_user, superuser=True, name="boss")
    for url in (
        reverse("receipt_list"),
        reverse("receipt_detail", args=[receipt.pk]),
        reverse("receipt_create"),
    ):
        assert "—" not in client.get(url).content.decode()
