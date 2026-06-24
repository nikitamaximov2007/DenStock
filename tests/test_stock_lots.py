"""Слой 9 — количественный учёт (StockLot).

Покрывает обязательные проверки плана 09-layer-9-stock-lots.md §10.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockLot
from apps.inventory.services import (
    InventoryError,
    create_stock_lot,
    distributed_qty,
    remaining_qty,
    update_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
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
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Крепёж")
    unit = Unit.objects.get(name="Штука")
    bulk = PartType.objects.create(
        name="Болт", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    serial = PartType.objects.create(
        name="Насос", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    loc1 = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка B", code="B-01", storage_allowed=True, is_active=True
    )
    loc_bad = StorageLocation.objects.create(
        name="Зона списания", code="WO", storage_allowed=False, is_active=True
    )
    return {
        "sup": sup, "bulk": bulk, "serial": serial,
        "loc1": loc1, "loc2": loc2, "loc_bad": loc_bad,
    }


def _finalized_line(refs, admin, *, part=None, quantity="10", unit_cost="50", shipping="100"):
    part = part or refs["bulk"]
    batch = Batch.objects.create(supplier=refs["sup"], shipping_cost=Decimal(shipping))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


# --- Сервис создания ---------------------------------------------------------


def test_create_lot_for_bulk_part(refs, admin):
    line = _finalized_line(refs, admin)
    lot = create_stock_lot(line, refs["loc1"], Decimal("4"))
    assert lot.status == StockLot.Status.RECEIVING
    assert lot.quantity == Decimal("4.000")
    assert lot.initial_quantity == Decimal("4.000")
    assert lot.batch == line.batch
    assert lot.location == refs["loc1"]


def test_cannot_create_for_serial_part(refs, admin):
    line = _finalized_line(refs, admin, part=refs["serial"])
    with pytest.raises(InventoryError):
        create_stock_lot(line, refs["loc1"], Decimal("1"))


def test_cannot_create_from_unfinalized_batch(refs, admin):
    batch = Batch.objects.create(supplier=refs["sup"], status=Batch.Status.ACCEPTED)
    line = BatchLine.objects.create(
        batch=batch, part_type=refs["bulk"],
        quantity=Decimal("10"), unit_cost_currency=Decimal("50"),
    )
    assert batch.cost_finalized is False
    with pytest.raises(InventoryError):
        create_stock_lot(line, refs["loc1"], Decimal("1"))


def test_landed_unit_cost_copied_and_frozen(refs, admin):
    # qty 10, unit 50 => base 500; shipping 100 => landed_total 600; landed_unit 60.
    line = _finalized_line(refs, admin, quantity="10", unit_cost="50", shipping="100")
    assert line.landed_unit_cost_rub == Decimal("60.00")
    lot = create_stock_lot(line, refs["loc1"], Decimal("10"))
    assert lot.landed_unit_cost_rub == Decimal("60.00")


def test_location_must_allow_storage(refs, admin):
    line = _finalized_line(refs, admin)
    with pytest.raises(InventoryError):
        create_stock_lot(line, refs["loc_bad"], Decimal("1"))


def test_quantity_must_be_positive(refs, admin):
    line = _finalized_line(refs, admin)
    with pytest.raises(InventoryError):
        create_stock_lot(line, refs["loc1"], Decimal("0"))
    with pytest.raises(InventoryError):
        create_stock_lot(line, refs["loc2"], Decimal("-3"))


def test_cannot_exceed_batch_line_quantity(refs, admin):
    line = _finalized_line(refs, admin, quantity="10")
    create_stock_lot(line, refs["loc1"], Decimal("6"))
    with pytest.raises(InventoryError):
        create_stock_lot(line, refs["loc2"], Decimal("5"))  # 6 + 5 > 10


def test_split_line_into_several_locations(refs, admin):
    line = _finalized_line(refs, admin, quantity="10")
    create_stock_lot(line, refs["loc1"], Decimal("6"))
    create_stock_lot(line, refs["loc2"], Decimal("4"))
    assert StockLot.objects.filter(batch_line=line).count() == 2
    assert distributed_qty(line) == Decimal("10.000")
    assert remaining_qty(line) == Decimal("0.000")


def test_same_line_same_location_rejected(refs, admin):
    line = _finalized_line(refs, admin, quantity="10")
    create_stock_lot(line, refs["loc1"], Decimal("3"))
    with pytest.raises(InventoryError):
        create_stock_lot(line, refs["loc1"], Decimal("2"))


def test_progress_distributed_and_remaining(refs, admin):
    line = _finalized_line(refs, admin, quantity="10")
    assert distributed_qty(line) == Decimal("0")
    create_stock_lot(line, refs["loc1"], Decimal("4"))
    assert distributed_qty(line) == Decimal("4.000")
    assert remaining_qty(line) == Decimal("6.000")


def test_initial_quantity_frozen_on_edit(refs, admin):
    line = _finalized_line(refs, admin, quantity="10")
    lot = create_stock_lot(line, refs["loc1"], Decimal("10"))
    update_stock_lot(lot, location=refs["loc1"], quantity=Decimal("6"), note="коррекция")
    lot.refresh_from_db()
    assert lot.quantity == Decimal("6.000")
    assert lot.initial_quantity == Decimal("10.000")  # не изменилось


def test_no_movement_or_balance_models():
    import apps.inventory.models as inv

    assert not hasattr(inv, "StockMovement")
    assert not hasattr(inv, "StockBalance")


# --- Экраны и права ----------------------------------------------------------


def test_storekeeper_can_create_via_view(make_user, client, refs, admin):
    line = _finalized_line(refs, admin)
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(
        reverse("lot_create", args=[line.pk]),
        {"location": refs["loc1"].pk, "quantity": "5"},
    )
    assert resp.status_code == 302
    assert StockLot.objects.filter(batch_line=line).count() == 1


def test_cost_hidden_from_storekeeper(make_user, client, refs, admin):
    line = _finalized_line(refs, admin)  # landed_unit = 60
    lot = create_stock_lot(line, refs["loc1"], Decimal("10"))
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert "60" not in html

    client.logout()
    client.login(username="admin", password=PASSWORD)
    admin_html = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert "60" in admin_html


def test_seller_cannot_view_lots(make_user, client):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("lot_list")).status_code == 403


def test_viewer_can_view_but_not_manage(make_user, client, refs, admin):
    line = _finalized_line(refs, admin)
    create_stock_lot(line, refs["loc1"], Decimal("3"))
    make_user("nabl", role=roles.VIEWER)
    client.login(username="nabl", password=PASSWORD)
    assert client.get(reverse("lot_list")).status_code == 200
    resp = client.post(
        reverse("lot_create", args=[line.pk]),
        {"location": refs["loc2"].pk, "quantity": "1"},
    )
    assert resp.status_code == 403


def test_nav_section_visibility(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    make_user("prodavec", role=roles.SELLER)

    client.login(username="sklad", password=PASSWORD)
    assert "Лоты" in client.get(reverse("dashboard")).content.decode()

    client.logout()
    client.login(username="prodavec", password=PASSWORD)
    assert "Лоты" not in client.get(reverse("dashboard")).content.decode()
