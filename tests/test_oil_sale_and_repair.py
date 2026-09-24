"""Масло в Sale/Repair: объём в литрах, цена за литр, снимки, отчёт, возврат.

Формула (apps.inventory.pricing.oil_line_amount_rub):
    цена_за_литр = цена_упаковки / объём_упаковки   (без округления)
    сумма_строки = цена_за_литр × объём               (округление один раз)
"""
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.catalog.models import Category, Manufacturer, PartType, Unit
from apps.catalog_import.models import AftermarketCatalogPart
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    RepairError,
    add_oil_volume_to_repair_order,
    complete_repair_order,
    create_repair_order,
    repair_customer_line_amounts,
)
from apps.reports.services import Period, get_sales_report
from apps.returns.services import (
    ReturnError,
    add_repair_line_return,
    add_sale_line_return,
    create_return,
)
from apps.sales.services import (
    SaleError,
    add_oil_volume_to_sale,
    cancel_sale,
    complete_sale,
    create_sale,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from tests.customs_support import remember_customs


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


def _oil_lot(part, admin, *, package_qty="10", location_code="S60-D01-C01"):
    supplier = Supplier.objects.create(name=f"Поставщик {part.pk}")
    location = StorageLocation.objects.create(
        name="Масло", code=location_code, storage_allowed=True
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(package_qty), unit_cost_currency=Decimal("5")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(package_qty))
    receive_stock_lot(lot, by=admin)
    return lot, location


@pytest.fixture
def oil_part(category, liter_unit):
    part = PartType.objects.create(
        name="Масло Motul 5W-40", category=category, unit=liter_unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True, oil_package_volume_l=Decimal("4"),
        recommended_price=Decimal("1000"),
    )
    remember_customs(part)
    return part


@pytest.fixture
def oil_lot(oil_part, admin):
    lot, location = _oil_lot(oil_part, admin, package_qty="10")
    return lot


# --- Формула цены -----------------------------------------------------------


def test_4l_1000_gives_250_per_liter(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    line = add_oil_volume_to_sale(sale, oil_lot, "1", by=admin)
    assert line.unit_price == Decimal("250.00")
    assert line.total_price == Decimal("250.00")


def test_03l_is_75(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    line = add_oil_volume_to_sale(sale, oil_lot, "0.3", by=admin)
    assert line.total_price == Decimal("75.00")


def test_15l_is_375(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    line = add_oil_volume_to_sale(sale, oil_lot, "1.5", by=admin)
    assert line.total_price == Decimal("375.00")


def test_001l_millilitre_precision_accepted(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    line = add_oil_volume_to_sale(sale, oil_lot, "0.001", by=admin)
    assert line.quantity == Decimal("0.001")


def test_no_cumulative_rounding_drift_on_uneven_package(category, liter_unit, admin):
    """3 L упаковка за 1000 ₽: 1000/3 = 333.333...; продажа ВСЕХ 3 л должна
    дать ровно 1000.00, а не 999.99 от округления цены за литр до продажи."""
    part = PartType.objects.create(
        name="Масло неровное", category=category, unit=liter_unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True, oil_package_volume_l=Decimal("3"),
        recommended_price=Decimal("1000"),
    )
    remember_customs(part)
    lot, _ = _oil_lot(part, admin, package_qty="3")
    sale = create_sale(customer_name="К", by=admin)
    line = add_oil_volume_to_sale(sale, lot, "3", by=admin)
    assert line.total_price == Decimal("1000.00")
    # Отображаемая цена за литр округлена отдельно и НЕ используется для суммы.
    assert line.unit_price == Decimal("333.33")


# --- Наличие / доступность ---------------------------------------------------


def test_available_stock_is_total_liters_not_piece_count(oil_part, oil_lot):
    assert oil_lot.quantity == Decimal("10")


def test_insufficient_oil_volume_blocked(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    with pytest.raises(SaleError):
        add_oil_volume_to_sale(sale, oil_lot, "10.001", by=admin)


def test_non_oil_lot_rejected_by_oil_entry_point(category, liter_unit, admin):
    part = PartType.objects.create(
        name="Обычная деталь", category=category, unit=liter_unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    remember_customs(part)
    lot, _ = _oil_lot(part, admin, package_qty="5")
    sale = create_sale(customer_name="К", by=admin)
    with pytest.raises(SaleError):
        add_oil_volume_to_sale(sale, lot, "1", by=admin)


# --- Draft/finalization -------------------------------------------------------


def test_draft_oil_sale_does_not_move_stock(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    add_oil_volume_to_sale(sale, oil_lot, "0.3", by=admin)
    oil_lot.refresh_from_db()
    assert oil_lot.quantity == Decimal("10")


def test_completion_decrements_exact_liters(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    add_oil_volume_to_sale(sale, oil_lot, "0.3", by=admin)
    complete_sale(sale, by=admin)
    oil_lot.refresh_from_db()
    assert oil_lot.quantity == Decimal("9.700")


def test_double_completion_does_not_double_decrement(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    add_oil_volume_to_sale(sale, oil_lot, "0.3", by=admin)
    complete_sale(sale, by=admin)
    with pytest.raises(SaleError):
        complete_sale(sale, by=admin)
    oil_lot.refresh_from_db()
    assert oil_lot.quantity == Decimal("9.700")


def test_completion_freeze_is_idempotent_with_draft_amount(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    line = add_oil_volume_to_sale(sale, oil_lot, "0.3", by=admin)
    draft_total = line.total_price
    complete_sale(sale, by=admin)
    line.refresh_from_db()
    assert line.total_price == draft_total == Decimal("75.00")


# --- Historical snapshot immutability ----------------------------------------


def test_changing_current_price_does_not_change_old_oil_line(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    line = add_oil_volume_to_sale(sale, oil_lot, "1", by=admin)
    complete_sale(sale, by=admin)
    line.refresh_from_db()

    oil_part.recommended_price = Decimal("2000")
    oil_part.save(update_fields=["recommended_price"])

    line.refresh_from_db()
    assert line.total_price == Decimal("250.00")
    assert line.oil_package_price_rub_snapshot == Decimal("1000")


def test_package_volume_change_blocked_once_stocked_so_history_is_safe(oil_part, oil_lot):
    oil_part.oil_package_volume_l = Decimal("2")
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        oil_part.full_clean()


# --- Sale report integration --------------------------------------------------


def test_oil_revenue_known_cost_and_profit_invariant(category, liter_unit, admin):
    part = PartType.objects.create(
        name="Масло с дилерской ценой", category=category, unit=liter_unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True, oil_package_volume_l=Decimal("4"),
        recommended_price=Decimal("1000"),
    )
    manufacturer = Manufacturer.objects.create(name="Aftermarket")
    AftermarketCatalogPart.objects.create(
        source=AftermarketCatalogPart.SOURCE_DEALER_2023, part=part,
        manufacturer=manufacturer, manufacturer_number="OIL-1", source_description="t",
        dealer_cost_usd=Decimal("10"),  # "package" dealer price, symmetric with recommended_price
    )
    remember_customs(part)
    lot, _ = _oil_lot(part, admin, package_qty="10")
    sale = create_sale(customer_name="К", by=admin)
    add_oil_volume_to_sale(sale, lot, "2", by=admin)
    complete_sale(sale, by=admin)

    report = get_sales_report(
        Period(timezone.localdate() - timezone.timedelta(days=1), timezone.localdate(), "")
    )
    assert report.revenue == Decimal("500.00")  # 2 L * 250 ₽/L
    assert report.profit_unavailable_lines == 0
    assert report.known_revenue == report.revenue
    assert report.revenue - report.cost == report.profit


def test_oil_line_without_dealer_link_is_disclosed_unknown_not_zero(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    add_oil_volume_to_sale(sale, oil_lot, "1", by=admin)
    complete_sale(sale, by=admin)

    report = get_sales_report(
        Period(timezone.localdate() - timezone.timedelta(days=1), timezone.localdate(), "")
    )
    assert report.revenue == Decimal("250.00")
    assert report.profit_unavailable_lines == 1
    assert report.known_revenue == Decimal("0.00")
    assert report.cost == Decimal("0.00")


# --- Returns: oil is not silently restored -----------------------------------


def test_oil_sale_line_return_is_rejected(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    line = add_oil_volume_to_sale(sale, oil_lot, "1", by=admin)
    sale = complete_sale(sale, by=admin)
    line.refresh_from_db()
    ret = create_return(source=sale, by=admin)
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret, line, Decimal("1"), to_location=oil_lot.location,
            restock_status="available", by=admin,
        )


def test_cancel_sale_does_not_restore_oil_stock(oil_part, oil_lot, admin):
    sale = create_sale(customer_name="К", by=admin)
    add_oil_volume_to_sale(sale, oil_lot, "1", by=admin)
    complete_sale(sale, by=admin)
    oil_lot.refresh_from_db()
    before = oil_lot.quantity
    cancel_sale(sale, by=admin, reason="ошибка", author="Тест")
    oil_lot.refresh_from_db()
    assert oil_lot.quantity == before  # НЕ восстановлено
    sale.refresh_from_db()
    assert sale.status == sale.Status.CANCELED


# --- Repair -------------------------------------------------------------------


def test_repair_asks_volume_and_computes_proportional_amount(oil_part, oil_lot, admin):
    order = create_repair_order(customer_name="К", by=admin)
    line = add_oil_volume_to_repair_order(order, oil_lot, "0.3", by=admin)
    assert line.customer_unit_price_rub == Decimal("250.00")
    assert line.oil_customer_amount_rub_snapshot == Decimal("75.00")


def test_repair_completion_decrements_exact_liters(oil_part, oil_lot, admin):
    order = create_repair_order(customer_name="К", by=admin)
    add_oil_volume_to_repair_order(order, oil_lot, "0.3", by=admin)
    complete_repair_order(order, by=admin)
    oil_lot.refresh_from_db()
    assert oil_lot.quantity == Decimal("9.700")


def test_repair_customer_amount_uses_frozen_snapshot(oil_part, oil_lot, admin):
    order = create_repair_order(customer_name="К", by=admin)
    line = add_oil_volume_to_repair_order(order, oil_lot, "0.3", by=admin)
    complete_repair_order(order, by=admin)
    line.refresh_from_db()
    amounts = repair_customer_line_amounts([line])
    assert amounts[line.pk] == Decimal("75.00")


def test_normal_repair_part_unaffected_by_oil_changes(category, liter_unit, admin):
    part = PartType.objects.create(
        name="Обычная деталь", category=category, unit=liter_unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    remember_customs(part)
    lot, _ = _oil_lot(part, admin, package_qty="5")
    order = create_repair_order(customer_name="К", by=admin)
    from apps.repairs.services import add_stock_lot_to_repair_order

    line = add_stock_lot_to_repair_order(
        order, lot, Decimal("2"), customer_unit_price_rub=Decimal("100"), by=admin
    )
    complete_repair_order(order, by=admin)
    line.refresh_from_db()
    assert line.oil_package_volume_l_snapshot is None
    assert line.oil_customer_amount_rub_snapshot is None


def test_repair_oil_return_is_rejected(oil_part, oil_lot, admin):
    order = create_repair_order(customer_name="К", by=admin)
    line = add_oil_volume_to_repair_order(order, oil_lot, "0.3", by=admin)
    order = complete_repair_order(order, by=admin)
    line.refresh_from_db()
    location = oil_lot.location
    ret = create_return(source=order, by=admin)
    with pytest.raises(ReturnError):
        add_repair_line_return(
            ret, line, Decimal("0.3"), to_location=location,
            restock_status="available", by=admin,
        )


def test_repair_oil_requires_package_price(category, liter_unit, admin):
    part = PartType.objects.create(
        name="Масло без цены", category=category, unit=liter_unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True, oil_package_volume_l=Decimal("4"),
    )
    remember_customs(part)
    lot, _ = _oil_lot(part, admin, package_qty="10")
    order = create_repair_order(customer_name="К", by=admin)
    with pytest.raises(RepairError):
        add_oil_volume_to_repair_order(order, lot, "1", by=admin)
