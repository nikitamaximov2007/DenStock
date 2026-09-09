"""Stage 8 — история движений показывает операторский адрес ячейки.

Экраны движений остались единственным местом, где сотрудник видел адрес в
хранимом виде S01-D03-C08, хотя весь остальной интерфейс уже показывает 1-3-8.
Рядом при этом стояла подсказка «(сейчас 1-3-8)» - в одной строке два разных
написания одного адреса.

Здесь меняется ТОЛЬКО представление. Снимок адреса в движении, code и barcode
ячейки, её первичный ключ и сами движения остаются прежними: историю мы не
переписываем, а показываем так же, как весь остальной склад.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockMovement
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    move_part_item,
    move_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
FROM_CODE = "S01-D03-C08"
TO_CODE = "S02-D01-C04"
FROM_SHORT = "1-3-8"
TO_SHORT = "2-1-4"
# Старый адрес с уровнем: короткой формы у него нет, и выдумывать её нельзя.
LEGACY_CODE = "S04-L03-D01-C04"


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
def boss(make_user):
    return make_user("boss", is_superuser=True)


def _cell(code):
    return StorageLocation.objects.create(
        name=code, code=code, storage_allowed=True, is_active=True
    )


def _batch_line(part, admin, *, quantity, serial=False):
    supplier = Supplier.objects.create(name=f"Поставщик {part.pk}-{quantity}")
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(str(quantity)),
        unit_cost_currency=Decimal("1"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def moved(db, boss):
    """Лот и экземпляр, каждый перемещён из одной ячейки в другую."""
    unit = Unit.objects.get(name="Штука")
    category = Category.objects.create(name="Вариатор")
    source, target = _cell(FROM_CODE), _cell(TO_CODE)

    bulk = PartType.objects.create(
        name="Болт", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=bulk, value="700100", kind=PartNumber.Kind.OEM)
    lot = create_stock_lot(_batch_line(bulk, boss, quantity=3), source, Decimal("3"))
    receive_stock_lot(lot, by=boss)
    move_stock_lot(lot, target, by=boss)

    serial = PartType.objects.create(
        name="Экземпляр", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=serial, value="700200", kind=PartNumber.Kind.OEM)
    item = create_part_items(
        _batch_line(serial, boss, quantity=1), 1, current_location=source
    )[0]
    receive_part_item(item, by=boss)
    move_part_item(item, target, by=boss)

    return {"boss": boss, "source": source, "target": target, "lot": lot, "item": item}


def _login(client, moved):
    client.force_login(moved["boss"])


def _card(body):
    """Верхняя карточка страницы: всё до таблицы истории движений."""
    marker = "Откуда"
    assert marker in body, "на странице нет истории движений"
    return body.split(marker, 1)[0]


def _history(body):
    """Только таблица истории движений.

    На карточках лота и экземпляра рядом стоит поле «Место» с полным путём по
    хранимым кодам (StorageLocation.full_path). Это отдельная поверхность, и
    трогать её здесь незачем: проверяем ровно ту таблицу, которую чинили.
    """
    marker = "Откуда"
    assert marker in body, "на странице нет истории движений"
    return body.split(marker, 1)[1]


def _assert_short_not_long(body, *, where):
    assert FROM_SHORT in body, f"{where}: нет короткого адреса откуда"
    assert TO_SHORT in body, f"{where}: нет короткого адреса куда"
    assert FROM_CODE not in body, f"{where}: остался хранимый вид {FROM_CODE}"
    assert TO_CODE not in body, f"{where}: остался хранимый вид {TO_CODE}"


# --- 1-4. Четыре экрана истории движений --------------------------------------------------


def test_movement_list_shows_the_operator_address(client, moved):
    _login(client, moved)
    body = client.get(reverse("movement_list")).content.decode()
    _assert_short_not_long(body, where="список движений")


def test_movement_detail_shows_the_operator_address(client, moved):
    _login(client, moved)
    movement = StockMovement.objects.filter(
        from_location=moved["source"], to_location=moved["target"]
    ).first()
    assert movement is not None, "перемещение не создано"
    body = client.get(reverse("movement_detail", args=[movement.pk])).content.decode()
    _assert_short_not_long(body, where="карточка движения")


def test_item_detail_history_shows_the_operator_address(client, moved):
    _login(client, moved)
    body = client.get(reverse("item_detail", args=[moved["item"].pk])).content.decode()
    _assert_short_not_long(_history(body), where="история экземпляра")


def test_lot_detail_history_shows_the_operator_address(client, moved):
    _login(client, moved)
    body = client.get(reverse("lot_detail", args=[moved["lot"].pk])).content.decode()
    _assert_short_not_long(_history(body), where="история лота")


# --- 5. Legacy адрес не переписывается -----------------------------------------------------


def test_a_legacy_level_address_is_shown_unchanged(client, boss):
    """У S-L адреса короткой формы нет: сокращать его значило бы назвать
    другую ячейку."""
    unit = Unit.objects.get(name="Штука")
    legacy, target = _cell(LEGACY_CODE), _cell(TO_CODE)
    part = PartType.objects.create(
        name="Легаси", category=Category.objects.create(name="Легаси-кат"), unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    lot = create_stock_lot(_batch_line(part, boss, quantity=2), legacy, Decimal("2"))
    receive_stock_lot(lot, by=boss)
    move_stock_lot(lot, target, by=boss)

    client.force_login(boss)
    body = client.get(reverse("movement_list")).content.decode()
    assert LEGACY_CODE in body, "старый адрес пропал или был искажён"
    assert TO_SHORT in body


# --- 6-7. Данные не тронуты ----------------------------------------------------------------


def test_the_persisted_snapshot_is_untouched(client, moved):
    """Показ короткой формы не переписывает ни ячейку, ни движение."""
    _login(client, moved)
    client.get(reverse("movement_list"))
    client.get(reverse("lot_detail", args=[moved["lot"].pk]))

    source = StorageLocation.objects.get(pk=moved["source"].pk)
    target = StorageLocation.objects.get(pk=moved["target"].pk)
    assert source.code == FROM_CODE
    assert target.code == TO_CODE
    assert source.barcode == f"LOC:{FROM_CODE}"
    assert target.barcode == f"LOC:{TO_CODE}"
    movement = StockMovement.objects.filter(
        from_location=source, to_location=target
    ).first()
    assert movement.from_location_id == source.pk
    assert movement.to_location_id == target.pk


def test_rendering_creates_no_duplicate_location(client, moved):
    before = StorageLocation.objects.count()
    codes = set(StorageLocation.objects.values_list("code", flat=True))
    _login(client, moved)
    for url in (
        reverse("movement_list"),
        reverse("item_detail", args=[moved["item"].pk]),
        reverse("lot_detail", args=[moved["lot"].pk]),
    ):
        client.get(url)
    assert StorageLocation.objects.count() == before
    assert set(StorageLocation.objects.values_list("code", flat=True)) == codes
    assert not StorageLocation.objects.filter(code__in=[FROM_SHORT, TO_SHORT]).exists()


def test_a_renamed_cell_still_shows_both_addresses_in_short_form(client, moved):
    """Переименование показывает исторический и текущий адрес - оба короткие."""
    from apps.warehouse.services import rename_storage_location

    rename_storage_location(
        moved["target"], new_code="S02-D01-C09", expected_code=TO_CODE, by=moved["boss"]
    )
    _login(client, moved)
    body = client.get(reverse("movement_list")).content.decode()
    assert TO_SHORT in body, "исторический адрес не показан в коротком виде"
    assert "2-1-9" in body, "текущий адрес не показан в коротком виде"
    assert TO_CODE not in body
    assert "S02-D01-C09" not in body


# --- Поле «Место» на карточках лота и экземпляра ------------------------------------------


def test_item_card_shows_the_operator_address(client, moved):
    """Главное поле «где деталь» читается так же, как история под ним."""
    _login(client, moved)
    card = _card(client.get(reverse("item_detail", args=[moved["item"].pk])).content.decode())
    assert TO_SHORT in card, "карточка экземпляра не показывает операторский адрес"
    assert TO_CODE not in card, f"в карточке остался хранимый вид {TO_CODE}"


def test_lot_card_shows_the_operator_address(client, moved):
    _login(client, moved)
    card = _card(client.get(reverse("lot_detail", args=[moved["lot"].pk])).content.decode())
    assert TO_SHORT in card
    assert TO_CODE not in card


def test_the_card_drops_the_whole_stored_ancestor_chain(client, boss):
    """У ячейки с родителями полный путь печатал адрес трижды: S02 / S02-D01 /
    S02-D01-C04. Короткий код несёт те же стеллаж, ящик и ячейку одной строкой."""
    from apps.warehouse.addresses import get_or_create_location

    cell = get_or_create_location(TO_CODE)
    assert cell.parent is not None, "ячейка без родителя не проверяет цепочку"
    chain = cell.full_path
    assert chain == "S02 / S02-D01 / S02-D01-C04"

    part = PartType.objects.create(
        name="Цепочка", category=Category.objects.create(name="Цепочка-кат"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    lot = create_stock_lot(_batch_line(part, boss, quantity=1), cell, Decimal("1"))
    receive_stock_lot(lot, by=boss)

    client.force_login(boss)
    body = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert TO_SHORT in body
    assert chain not in body, "полный путь по хранимым кодам всё ещё печатается"


def test_a_legacy_cell_is_shown_unchanged_on_the_card(client, boss):
    """Для S-L адреса короткой формы нет: показываем его как есть."""
    legacy = _cell(LEGACY_CODE)
    part = PartType.objects.create(
        name="Легаси-карточка", category=Category.objects.create(name="Легаси-карточка-кат"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    lot = create_stock_lot(_batch_line(part, boss, quantity=1), legacy, Decimal("1"))
    receive_stock_lot(lot, by=boss)

    client.force_login(boss)
    body = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert LEGACY_CODE in body, "старый адрес пропал или был искажён"


def test_the_card_change_touches_no_stored_identity(client, moved):
    """Показ короткой формы на карточке ничего не переписывает."""
    _login(client, moved)
    before = StorageLocation.objects.count()
    client.get(reverse("item_detail", args=[moved["item"].pk]))
    client.get(reverse("lot_detail", args=[moved["lot"].pk]))

    target = StorageLocation.objects.get(pk=moved["target"].pk)
    assert target.code == TO_CODE
    assert target.barcode == f"LOC:{TO_CODE}"
    assert target.full_path.endswith(TO_CODE), "свойство full_path не должно меняться"
    assert StorageLocation.objects.count() == before
    assert not StorageLocation.objects.filter(code=TO_SHORT).exists()
