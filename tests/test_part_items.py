"""Слой 8 — поштучный учёт (PartItem).

Покрывает обязательные проверки плана 08-layer-8-part-items.md §11.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import PartItem
from apps.inventory.services import InventoryError, create_part_items
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
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    serial = PartType.objects.create(
        name="Насос", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    serial2 = PartType.objects.create(
        name="Стартер", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL
    )
    bulk = PartType.objects.create(
        name="Болт", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK
    )
    loc_ok = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    loc_bad = StorageLocation.objects.create(
        name="Приёмка", code="RCV", storage_allowed=False, is_active=True
    )
    return {
        "sup": sup, "serial": serial, "serial2": serial2, "bulk": bulk,
        "loc_ok": loc_ok, "loc_bad": loc_bad,
    }


def _finalized_line(refs, admin, *, part=None, quantity="2", unit_cost="100", shipping="40"):
    part = part or refs["serial"]
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


def test_create_item_for_serial_part(refs, admin):
    line = _finalized_line(refs, admin)
    items = create_part_items(line, 1)
    assert len(items) == 1
    item = items[0]
    assert item.status == PartItem.Status.RECEIVING
    assert item.internal_number.startswith("DS-")
    assert item.batch == line.batch


def test_cannot_create_for_bulk_part(refs, admin):
    line = _finalized_line(refs, admin, part=refs["bulk"])
    with pytest.raises(InventoryError):
        create_part_items(line, 1)


def test_cannot_create_from_unfinalized_batch(refs, admin):
    batch = Batch.objects.create(supplier=refs["sup"], status=Batch.Status.ACCEPTED)
    line = BatchLine.objects.create(
        batch=batch, part_type=refs["serial"],
        quantity=Decimal("2"), unit_cost_currency=Decimal("100"),
    )
    assert batch.cost_finalized is False
    with pytest.raises(InventoryError):
        create_part_items(line, 1)


def test_landed_cost_copied_and_frozen(refs, admin):
    # quantity 2, unit 100 => base 200; shipping 40 => landed_total 240; landed_unit 120.
    line = _finalized_line(refs, admin, quantity="2", unit_cost="100", shipping="40")
    assert line.landed_unit_cost_rub == Decimal("120.00")
    item = create_part_items(line, 1)[0]
    assert item.landed_cost_rub == Decimal("120.00")


def test_internal_number_unique_and_sequential(refs, admin):
    line = _finalized_line(refs, admin, quantity="3")
    items = create_part_items(line, 3)
    numbers = [i.internal_number for i in items]
    assert len(set(numbers)) == 3
    assert all(n.startswith("DS-") for n in numbers)


def test_internal_barcode_format_and_unique(refs, admin):
    line = _finalized_line(refs, admin)
    item = create_part_items(line, 1)[0]
    assert item.internal_barcode == f"ITEM:{item.internal_number}"
    assert PartItem.objects.filter(internal_barcode=item.internal_barcode).count() == 1


def test_cannot_exceed_quantity(refs, admin):
    line = _finalized_line(refs, admin, quantity="2")
    create_part_items(line, 2)
    with pytest.raises(InventoryError):
        create_part_items(line, 1)


def test_location_must_allow_storage(refs, admin):
    line = _finalized_line(refs, admin, quantity="2")
    with pytest.raises(InventoryError):
        create_part_items(line, 1, current_location=refs["loc_bad"])
    item = create_part_items(line, 1, current_location=refs["loc_ok"])[0]
    assert item.current_location == refs["loc_ok"]


def test_serial_saved_and_unique_per_parttype(refs, admin):
    line = _finalized_line(refs, admin, quantity="3")
    item = create_part_items(line, 1, serial_number="SN-1")[0]
    assert item.serial_number == "SN-1"
    # Тот же серийник у той же детали — запрет.
    with pytest.raises(InventoryError):
        create_part_items(line, 1, serial_number="SN-1")
    # Тот же серийник у ДРУГОЙ детали — допустимо.
    line2 = _finalized_line(refs, admin, part=refs["serial2"], quantity="1")
    other = create_part_items(line2, 1, serial_number="SN-1")[0]
    assert other.serial_number == "SN-1"


# --- Экраны и права ----------------------------------------------------------


def test_storekeeper_can_create_via_view(make_user, client, refs, admin):
    line = _finalized_line(refs, admin)
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("item_create", args=[line.pk]), {"serial_number": "SN-9"})
    assert resp.status_code == 302
    assert PartItem.objects.filter(batch_line=line).count() == 1


def test_cost_hidden_from_storekeeper(make_user, client, refs, admin):
    line = _finalized_line(refs, admin)  # landed_unit = 120
    item = create_part_items(line, 1)[0]
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("item_detail", args=[item.pk])).content.decode()
    assert "120" not in html

    client.logout()
    client.login(username="admin", password=PASSWORD)
    admin_html = client.get(reverse("item_detail", args=[item.pk])).content.decode()
    assert "120" in admin_html


def test_seller_cannot_view_inventory(make_user, client):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("item_list")).status_code == 403


def test_viewer_can_view_but_not_manage(make_user, client, refs, admin):
    line = _finalized_line(refs, admin)
    create_part_items(line, 1)
    make_user("nabl", role=roles.VIEWER)
    client.login(username="nabl", password=PASSWORD)
    resp = client.get(reverse("item_list"))
    assert resp.status_code == 200
    # Наблюдатель не управляет — попытка создать запрещена.
    resp = client.post(reverse("item_create", args=[line.pk]), {"serial_number": ""})
    assert resp.status_code == 403


def test_nav_section_visibility(make_user, client):
    make_user("sklad", role=roles.STOREKEEPER)
    make_user("prodavec", role=roles.SELLER)

    client.login(username="sklad", password=PASSWORD)
    assert ">Склад<" in client.get(reverse("dashboard")).content.decode()
    assert "Экземпляры" in client.get(reverse("balance_list")).content.decode()

    client.logout()
    client.login(username="prodavec", password=PASSWORD)
    assert ">Склад<" not in client.get(reverse("dashboard")).content.decode()


def test_storekeeper_cannot_manage_batches_capability(make_user):
    # Контроль разделения прав: кладовщик управляет инвентарём, но не партиями.
    user = make_user("sklad", role=roles.STOREKEEPER)
    assert user.can_manage_inventory is True
    assert user.can_manage_batches is False
    assert user.can_view_purchase_cost is False
