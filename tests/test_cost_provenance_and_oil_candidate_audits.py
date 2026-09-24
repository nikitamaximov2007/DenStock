"""Два read-only аудита: происхождение себестоимости продаж и кандидаты на «масло».

Оба ничего не пишут - ни в SaleLine, ни в PartType.is_oil.
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.catalog.oil_candidate_audit import audit_oil_candidates, audit_oil_migration_readiness
from apps.catalog_import.models import AftermarketCatalogPart
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.reports.cost_provenance_audit import audit_sale_cost_provenance
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from tests.customs_support import remember_customs


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser("owner", "owner@example.test", "pass")


@pytest.fixture
def category(db):
    return Category.objects.create(name="Тест")


@pytest.fixture
def unit(db):
    return Unit.objects.get(name="Штука")


def _stock_lot(part, admin, *, qty="4"):
    supplier = Supplier.objects.create(name=f"Поставщик {part.pk}")
    location = StorageLocation.objects.create(
        name="Тест", code=f"S{part.pk}-D01-C01", storage_allowed=True
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(qty), unit_cost_currency=Decimal("10")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(qty))
    receive_stock_lot(lot, by=admin)
    return lot


# --- Cost provenance audit -------------------------------------------------


def test_cost_provenance_audit_classifies_live_and_unknown(category, unit, admin):
    with_link = PartType.objects.create(
        name="С каталожной связью", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    manufacturer = Manufacturer.objects.create(name="Aftermarket")
    AftermarketCatalogPart.objects.create(
        source=AftermarketCatalogPart.SOURCE_DEALER_2023, part=with_link,
        manufacturer=manufacturer, manufacturer_number="A-1", source_description="t",
        dealer_cost_usd=Decimal("100"),
    )
    without_link = PartType.objects.create(
        name="Без связи", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    remember_customs(with_link, without_link)

    lot1 = _stock_lot(with_link, admin)
    sale1 = create_sale(customer_name="К1", by=admin)
    add_stock_lot_to_sale(sale1, lot1, Decimal("1"), unit_price=Decimal("16000"), by=admin)
    complete_sale(sale1, by=admin)

    lot2 = _stock_lot(without_link, admin)
    sale2 = create_sale(customer_name="К2", by=admin)
    add_stock_lot_to_sale(sale2, lot2, Decimal("1"), unit_price=Decimal("500"), by=admin)
    complete_sale(sale2, by=admin)

    report = audit_sale_cost_provenance()
    assert report.total_lines == 2
    assert report.live_count == 1
    assert report.unknown_count == 1
    assert report.legacy_105_count == 0
    assert report.live_known_revenue == Decimal("16000")
    assert report.unknown_revenue == Decimal("500")

    # Ничего не записано на строки/продажи - аудит только читает.
    sale1.refresh_from_db()
    assert sale1.status == sale1.Status.COMPLETED


def test_cost_provenance_audit_command_runs_read_only(category, unit, admin):
    part = PartType.objects.create(
        name="Деталь", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    remember_customs(part)
    lot = _stock_lot(part, admin)
    sale = create_sale(customer_name="К", by=admin)
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("500"), by=admin)
    complete_sale(sale, by=admin)

    out = StringIO()
    call_command("audit_sale_cost_provenance", stdout=out)
    text = out.getvalue()
    assert "только чтение" in text
    assert "Всего проведённых строк: 1" in text


# --- Oil candidate audit ---------------------------------------------------


def test_oil_candidate_audit_flags_337_prefix_but_never_writes(category, unit):
    candidate = PartType.objects.create(
        name="Похоже на масло", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=candidate, value="337-123-456", kind=PartNumber.Kind.OEM)

    unrelated = PartType.objects.create(
        name="Точно не масло", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=unrelated, value="420-999-000", kind=PartNumber.Kind.OEM)

    already_marked = PartType.objects.create(
        name="Уже масло", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True, oil_package_volume_l=Decimal("4"),
    )
    PartNumber.objects.create(part=already_marked, value="337-000-111", kind=PartNumber.Kind.OEM)

    report = audit_oil_candidates()
    ids = {row.part_type_id for row in report.rows}
    assert candidate.pk in ids
    assert unrelated.pk not in ids
    # Уже отмеченные не предлагаются повторно - им нечего проверять.
    assert already_marked.pk not in ids
    assert report.already_oil_count == 1

    candidate.refresh_from_db()
    assert candidate.is_oil is False  # аудит ничего не изменил


def test_oil_candidate_audit_command_runs_read_only(category, unit):
    part = PartType.objects.create(
        name="Кандидат", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=part, value="3371234", kind=PartNumber.Kind.OEM)

    out = StringIO()
    call_command("audit_oil_candidates", stdout=out)
    text = out.getvalue()
    assert "только чтение" in text
    assert "Кандидатов" in text
    part.refresh_from_db()
    assert part.is_oil is False


def test_oil_candidate_status_flags_history_as_needs_owner_review(
    category, unit, admin
):
    safe_candidate = PartType.objects.create(
        name="Свежий кандидат", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=safe_candidate, value="3379999", kind=PartNumber.Kind.OEM)

    risky_candidate = PartType.objects.create(
        name="Кандидат с остатком", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=risky_candidate, value="3378888", kind=PartNumber.Kind.OEM)
    supplier = Supplier.objects.create(name="Поставщик кандидата")
    location = StorageLocation.objects.create(
        name="Кандидат", code="S80-D01-C01", storage_allowed=True
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=risky_candidate, quantity=Decimal("5"),
        unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("5"))
    receive_stock_lot(lot, by=admin)

    report = audit_oil_candidates()
    statuses = {row.part_type_id: row.candidate_status for row in report.rows}
    assert statuses[safe_candidate.pk] == "safe_to_mark"
    assert statuses[risky_candidate.pk] == "needs_owner_review"
    assert report.safe_to_mark_count == 1
    assert report.needs_owner_review_count == 1


def test_oil_migration_readiness_reports_configuration_and_usage(category, unit, admin):
    configured = PartType.objects.create(
        name="Настроенное масло", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True, oil_package_volume_l=Decimal("4"), recommended_price=Decimal("1000"),
    )
    supplier = Supplier.objects.create(name="Поставщик готового масла")
    location = StorageLocation.objects.create(
        name="Готовое масло", code="S81-D01-C01", storage_allowed=True
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch, part_type=configured, quantity=Decimal("10"),
        unit_cost_currency=Decimal("5"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("10"))
    receive_stock_lot(lot, by=admin)
    remember_customs(configured)
    sale = create_sale(customer_name="К", by=admin)
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("250"), by=admin)
    complete_sale(sale, by=admin)

    report = audit_oil_migration_readiness()
    row = next(r for r in report.rows if r.part_type_id == configured.pk)
    assert row.configuration_status == "ok"
    assert row.stock_lot_count == 1
    assert row.sale_line_count == 1
    assert row.available_liters == "9.000"


def test_oil_candidates_command_prints_status_section(category, unit, admin):
    part = PartType.objects.create(
        name="Готовое масло для команды", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        is_oil=True, oil_package_volume_l=Decimal("4"), recommended_price=Decimal("1000"),
    )
    out = StringIO()
    call_command("audit_oil_candidates", stdout=out)
    text = out.getvalue()
    assert "статус конфигурации" in text
    assert part.name in text
