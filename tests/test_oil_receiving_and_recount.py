"""Масло в приёмке «найдено», очереди сканера и пересчёте участка.

«+1 за скан» подходит для штучной детали, но не для масла: целое число там
означало бы литры неоднозначно (канистра? литр?) и могло бы молча исказить
остаток. Эти пути либо отказывают явно (приёмка/очередь), либо создают
строку без автоинкремента и требуют ручного ввода объёма (пересчёт участка).
Обычная приёмка партии (Batch/BatchLine) и формальная инвентаризация
(InventoryCountDocument) уже прекрасно принимают дробный литраж без всяких
изменений - это подтверждают test_oil_sale_and_repair.py и test_stocktaking.py.
"""
from decimal import Decimal

import pytest

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.core.receiving_queue import ReceivingCandidate, ReceivingQueueError, add_candidate
from apps.inventory.services import (
    InventoryError,
    _post_found_stock_group,
    create_stock_lot,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.stocktaking.models import SectionRecountLine
from apps.stocktaking.section_recount import (
    create_section_recount,
    record_section_scan,
    set_section_line_quantity,
    start_section_recount,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser("owner", "owner@example.test", "pass")


@pytest.fixture
def category(db):
    return Category.objects.create(name="Масла")


@pytest.fixture
def liter_unit(db):
    unit, _ = Unit.objects.get_or_create(name="Литр", defaults={"short_name": "л"})
    return unit


@pytest.fixture
def oil_part(category, liter_unit):
    part = PartType.objects.create(
        name="Масло пересчёта", category=category, unit=liter_unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True, oil_package_volume_l=Decimal("4"),
        recommended_price=Decimal("1000"),
    )
    PartNumber.objects.create(
        part=part, value="OIL-0001", kind=PartNumber.Kind.OEM, is_primary=True
    )
    return part


@pytest.fixture
def oil_lot(oil_part, admin):
    supplier = Supplier.objects.create(name="Поставщик масла")
    location = StorageLocation.objects.create(
        name="Масло-recount", code="S70-D01-C01", storage_allowed=True
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=oil_part, quantity=Decimal("10"), unit_cost_currency=Decimal("5")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("10"))
    receive_stock_lot(lot, by=admin)
    return lot


# --- Found-stock batch queue: explicit refusal, no silent "+1 L" ------------


def test_found_stock_group_rejects_oil(oil_part, oil_lot, admin):
    with pytest.raises(InventoryError):
        _post_found_stock_group(
            entries=[{"part_id": oil_part.pk, "quantity": "1"}],
            location=oil_lot.location,
            token="tok-oil-1",
            by=admin,
        )


def test_receiving_queue_rejects_scanning_oil(oil_part):
    session = {}
    candidate = ReceivingCandidate(
        source="brp", source_id=1, exact_number="OIL-0001",
        manufacturer="Motul", name=oil_part.name, part_id=oil_part.pk,
        unit_price=Decimal("1000"),
    )
    with pytest.raises(ReceivingQueueError):
        add_candidate(session, candidate)


# --- Section recount: no auto-increment, manual volume required -------------
#
# create_section_recount() без явного section_code разбирает свой аргумент
# как строгий canonical V2-адрес (Sxx-Dxx, без L/зон) - кроме ровно одного
# дефолтного значения SECTION_CODE, которое остальные тесты этого файла уже
# используют напрямую. Переиспользуем тот же приём здесь.


def _default_section_locations():
    from apps.stocktaking.section_recount import SECTION_CODE

    codes = [f"{SECTION_CODE}-C{number:02d}" for number in range(1, 11)]
    return [
        StorageLocation.objects.get_or_create(
            code=code, defaults={"name": code, "storage_allowed": True, "is_active": True}
        )[0]
        for code in codes
    ]


def test_section_recount_scan_creates_zero_quantity_line_for_oil(oil_part, admin):
    locations = _default_section_locations()
    supplier = Supplier.objects.create(name="Поставщик масла-пересчёт")
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=oil_part, quantity=Decimal("4"), unit_cost_currency=Decimal("5")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, locations[0], Decimal("4"))
    receive_stock_lot(lot, by=admin)

    doc = create_section_recount(by=admin)
    start_section_recount(doc)
    doc.refresh_from_db()

    scanned = record_section_scan(doc, cell_number=1, raw_value="OIL-0001", by=admin)
    assert scanned.quantity == Decimal("0")

    # Повторный скан НЕ увеличивает количество (не "+1 л").
    scanned_again = record_section_scan(doc, cell_number=1, raw_value="OIL-0001", by=admin)
    assert scanned_again.pk == scanned.pk
    assert scanned_again.quantity == Decimal("0")

    # Ручной ввод фактического объёма по-прежнему работает.
    updated = set_section_line_quantity(
        SectionRecountLine.objects.get(pk=scanned.pk), "3,700", by=admin
    )
    assert updated.quantity == Decimal("3.700")


def test_section_recount_normal_part_still_increments_by_one(admin):
    locations = _default_section_locations()
    category = Category.objects.create(name="Обычные-пересчёт")
    unit = Unit.objects.get(name="Штука")
    part = PartType.objects.create(
        name="Обычная деталь пересчёта", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=part, value="RC-N-0001", kind=PartNumber.Kind.OEM, is_primary=True
    )
    supplier = Supplier.objects.create(name="Поставщик обычный-пересчёт")
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("5"), unit_cost_currency=Decimal("10")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, locations[0], Decimal("5"))
    receive_stock_lot(lot, by=admin)

    doc = create_section_recount(by=admin)
    start_section_recount(doc)
    doc.refresh_from_db()

    scanned = record_section_scan(doc, cell_number=1, raw_value="RC-N-0001", by=admin)
    assert scanned.quantity == Decimal("1")
    scanned = record_section_scan(doc, cell_number=1, raw_value="RC-N-0001", by=admin)
    assert scanned.quantity == Decimal("2")


# --- Quick-action scanner (sell/reserve/repair in one step, and its cart) ---
#
# Both price a line from part.recommended_price as a PER-UNIT price. For oil
# that field means PER-PACKAGE, so treating it as a per-liter price would
# overcharge by the package volume's factor (e.g. 4x for a 4 L canister).
# Neither UI here has a volume input either - quantity defaults to 1 "unit".
# Both must refuse outright rather than silently misprice.


def test_quick_action_scanner_refuses_oil(oil_part, oil_lot, admin):
    from apps.actions.services import ActionError, perform_action

    with pytest.raises(ActionError, match="сканером действий"):
        perform_action(
            part=oil_part, location=oil_lot.location, action_type="sale",
            quantity="1", customer_comment="Клиент", by=admin,
        )


def test_scanner_cart_refuses_oil(oil_part, oil_lot, admin):
    from apps.actions.cart import ActionError, add_scan, open_cart

    cart = open_cart("sale", by=admin)
    with pytest.raises(ActionError, match="корзиной сканера"):
        add_scan(cart, oil_part, oil_lot.location, by=admin)


# --- Formal stocktaking (InventoryCountDocument): fractional liters --------


def test_inventory_count_accepts_fractional_oil_volume(oil_part, oil_lot, admin):
    from apps.stocktaking.services import (
        add_stock_lot_count_line,
        complete_inventory_count,
        create_inventory_count,
        update_counted_quantity,
    )

    doc = create_inventory_count(scope_location=oil_lot.location, by=admin)
    line = add_stock_lot_count_line(doc, oil_lot, by=admin)
    assert line.expected_quantity == Decimal("10")

    update_counted_quantity(line, "9.300", by=admin)
    line.refresh_from_db()
    assert line.counted_quantity == Decimal("9.300")
    assert line.difference == Decimal("-0.700")

    complete_inventory_count(doc, by=admin)
    oil_lot.refresh_from_db()
    assert oil_lot.quantity == Decimal("9.300")


# --- Search/barcode: oil resolves through the same identity path ----------


def test_barcode_and_article_resolve_oil_part(oil_part):
    from apps.core.part_lookup import resolve_part_lookup

    lookup = resolve_part_lookup("OIL-0001")
    assert lookup.found
    assert lookup.candidate.part.pk == oil_part.pk
    # Наличие уже в литрах, а не в "штуках" - lookup не гадает объём по коду.
    assert lookup.candidate.physical == Decimal("0")  # приёмки в этом тесте не было
