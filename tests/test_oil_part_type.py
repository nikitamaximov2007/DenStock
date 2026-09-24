"""Масло: PartType.is_oil / oil_package_volume_l (модель + форма).

Слой 1 плана "oil inventory": фундамент данных. Объём упаковки хранится в
том же Decimal(max_digits=12, decimal_places=3) поле, что и обычное
количество (StockLot.quantity/StockMovement.quantity/SaleLine.quantity/...),
но означает ЛИТРЫ, а не штуки - никакого нового столбца/типа не вводится.
"""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.catalog.forms import PartTypeForm
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.fixture
def category(db):
    return Category.objects.create(name="Масла")


@pytest.fixture
def unit(db):
    return Unit.objects.get(name="Штука")


def _oil_part(category, unit, **overrides):
    kwargs = dict(
        name="Масло Motul 5W-40",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True,
        oil_package_volume_l=Decimal("4.000"),
    )
    kwargs.update(overrides)
    return PartType(**kwargs)


# --- Модель --------------------------------------------------------------


def test_oil_part_requires_positive_package_volume(category, unit):
    part = _oil_part(category, unit, oil_package_volume_l=None)
    with pytest.raises(ValidationError):
        part.full_clean()


def test_oil_part_rejects_zero_package_volume(category, unit):
    part = _oil_part(category, unit, oil_package_volume_l=Decimal("0"))
    with pytest.raises(ValidationError):
        part.full_clean()


def test_non_oil_part_rejects_package_volume(category, unit):
    part = PartType(
        name="Обычная деталь", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=False, oil_package_volume_l=Decimal("1.000"),
    )
    with pytest.raises(ValidationError):
        part.full_clean()


def test_oil_part_must_be_bulk(category, unit):
    part = _oil_part(category, unit, tracking_mode=PartType.TrackingMode.SERIAL)
    with pytest.raises(ValidationError):
        part.full_clean()


def test_oil_part_saves_cleanly(category, unit):
    part = _oil_part(category, unit)
    part.full_clean()
    part.save()
    part.refresh_from_db()
    assert part.is_oil is True
    assert part.oil_package_volume_l == Decimal("4.000")


def test_db_constraint_rejects_oil_without_volume_bypassing_clean(category, unit):
    part = PartType.objects.create(
        name="Обычная деталь", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    # Прямой UPDATE в обход save()/clean() всё равно ловится CHECK-констрейнтом
    # на уровне БД, а не только Python-валидацией.
    with pytest.raises(IntegrityError), transaction.atomic():
        PartType.objects.filter(pk=part.pk).update(is_oil=True, oil_package_volume_l=None)


def test_ordinary_bulk_part_is_unaffected_by_default(category, unit):
    part = PartType.objects.create(
        name="Обычная деталь", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    assert part.is_oil is False
    assert part.oil_package_volume_l is None


# --- Guard: нельзя менять is_oil/объём после появления остатков/истории ---


def _stocked_bulk_part(category, unit, admin, *, is_oil=False, oil_package_volume_l=None):
    part = PartType(
        name="Деталь со складом", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=is_oil, oil_package_volume_l=oil_package_volume_l,
    )
    part.full_clean()
    part.save()
    supplier = Supplier.objects.create(name="Поставщик")
    location = StorageLocation.objects.create(
        name="Ячейка масла", code="S50-D01-C01", storage_allowed=True
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("10"), unit_cost_currency=Decimal("10")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("10"))
    receive_stock_lot(lot, by=admin)
    return part


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser("owner", "owner@example.test", "pass")


def test_cannot_mark_existing_stocked_part_as_oil(category, unit, admin):
    part = _stocked_bulk_part(category, unit, admin)
    part.is_oil = True
    part.oil_package_volume_l = Decimal("1.000")
    with pytest.raises(ValidationError):
        part.full_clean()


def test_cannot_unmark_existing_stocked_oil_part(category, unit, admin):
    part = _stocked_bulk_part(
        category, unit, admin, is_oil=True, oil_package_volume_l=Decimal("4.000")
    )
    part.is_oil = False
    part.oil_package_volume_l = None
    with pytest.raises(ValidationError):
        part.full_clean()


def test_cannot_change_package_volume_once_stock_exists(category, unit, admin):
    part = _stocked_bulk_part(
        category, unit, admin, is_oil=True, oil_package_volume_l=Decimal("4.000")
    )
    part.oil_package_volume_l = Decimal("1.000")
    with pytest.raises(ValidationError):
        part.full_clean()


def test_can_change_package_volume_before_any_stock(category, unit):
    part = _oil_part(category, unit)
    part.full_clean()
    part.save()
    part.oil_package_volume_l = Decimal("1.000")
    part.full_clean()  # не бросает - остатков/истории ещё нет
    part.save()
    part.refresh_from_db()
    assert part.oil_package_volume_l == Decimal("1.000")


def test_can_change_tracking_mode_is_blocked_once_stocked(category, unit, admin):
    part = _stocked_bulk_part(category, unit, admin)
    assert part.can_change_tracking_mode() is False


def test_can_change_tracking_mode_true_before_stock(category, unit):
    part = PartType.objects.create(
        name="Свежая деталь", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    assert part.can_change_tracking_mode() is True


# --- Форма -----------------------------------------------------------------


def test_form_accepts_comma_decimal_volume(category, unit):
    form = PartTypeForm(data={
        "name": "Масло Motul 5W-40",
        "category": category.pk,
        "unit": unit.pk,
        "tracking_mode": PartType.TrackingMode.BULK,
        "min_stock_level": "0",
        "is_oil": "on",
        "oil_package_volume_l": "4,5",
    })
    assert form.is_valid(), form.errors
    part = form.save()
    assert part.oil_package_volume_l == Decimal("4.5")


def test_form_rejects_oil_without_volume(category, unit):
    form = PartTypeForm(data={
        "name": "Масло без объёма",
        "category": category.pk,
        "unit": unit.pk,
        "tracking_mode": PartType.TrackingMode.BULK,
        "min_stock_level": "0",
        "is_oil": "on",
        "oil_package_volume_l": "",
    })
    assert not form.is_valid()


def test_form_ordinary_part_without_oil_fields_still_works(category, unit):
    form = PartTypeForm(data={
        "name": "Обычная деталь",
        "category": category.pk,
        "unit": unit.pk,
        "tracking_mode": PartType.TrackingMode.BULK,
        "min_stock_level": "0",
    })
    assert form.is_valid(), form.errors
    part = form.save()
    assert part.is_oil is False
    assert part.oil_package_volume_l is None
