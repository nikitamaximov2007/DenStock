from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest
from django.utils import timezone

from apps.catalog.models import Category, Manufacturer, PartType, Unit
from apps.catalog_import.models import AftermarketCatalogPart
from apps.inventory.pricing import resolve_effective_inventory_customer_price
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.reports.services import Period, get_sales_report
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation, ValuationSettings
from tests.customs_support import remember_customs


@pytest.fixture
def priced_sale(db, django_user_model):
    user = django_user_model.objects.create_superuser("owner", "owner@example.test", "pass")
    category = Category.objects.create(name="Тест")
    unit = Unit.objects.get(name="Штука")
    manufacturer = Manufacturer.objects.create(name="Aftermarket")
    part = PartType.objects.create(
        name="Тестовая деталь", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("14700"),
    )
    AftermarketCatalogPart.objects.create(
        source=AftermarketCatalogPart.SOURCE_DEALER_2023, part=part, manufacturer=manufacturer,
        manufacturer_number="A-100", source_description="test", dealer_cost_usd=Decimal("100"),
    )
    location = StorageLocation.objects.create(
        name="Тест", code="S99-D99-C99", storage_allowed=True
    )
    supplier = Supplier.objects.create(name="Поставщик")
    batch = Batch.objects.create(supplier=supplier)
    batch_line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("4"), unit_cost_currency=Decimal("10")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, user)
    batch_line.refresh_from_db()
    lot = create_stock_lot(batch_line, location, Decimal("4"))
    receive_stock_lot(lot, by=user)
    remember_customs(part)
    return user, part, lot, location


def _complete(user, lot, *, unit_price, quantity="1"):
    sale = create_sale(customer_name="Клиент", by=user)
    add_stock_lot_to_sale(sale, lot, Decimal(quantity), unit_price=Decimal(unit_price), by=user)
    return complete_sale(sale, by=user)


def test_future_sale_freezes_live_unmarked_rate_and_uses_customer_price(priced_sale):
    user, part, lot, _ = priced_sale
    settings = ValuationSettings.get()
    settings.current_usd_rate = Decimal("105")
    settings.save(update_fields=["current_usd_rate"])
    sale = _complete(user, lot, unit_price="16000")
    line = sale.lines.get()
    assert line.unmarked_unit_price_rub_snapshot == Decimal("10500")
    assert line.unmarked_dealer_unit_usd_snapshot == Decimal("100")
    assert line.unmarked_usd_rate_snapshot == Decimal("105")
    assert line.unmarked_price_source == "aftermarket"
    assert line.unit_price == Decimal("16000")  # actual historical transaction is untouched
    assert resolve_effective_inventory_customer_price(lot, Decimal("14700")) == Decimal("14700")

    settings.current_usd_rate = Decimal("150")
    settings.save(update_fields=["current_usd_rate"])
    part.aftermarket_catalog_entry.dealer_cost_usd = Decimal("50")
    part.aftermarket_catalog_entry.save(update_fields=["dealer_cost_usd"])
    line.refresh_from_db()
    report = get_sales_report(
        Period(timezone.localdate() - timedelta(days=1), timezone.localdate(), "")
    )
    assert line.unmarked_unit_price_rub_snapshot == Decimal("10500")
    assert report.revenue == Decimal("16000.00")
    assert report.known_revenue == Decimal("16000.00")
    assert report.cost == Decimal("10500.00")
    assert report.profit == Decimal("5500")
    assert report.known_revenue - report.cost == report.profit


def test_return_does_not_reduce_sales_report_revenue_cost_or_profit(priced_sale):
    """Возвраты - отдельный отчёт (get_returns_report); сводка продаж всегда
    считает исходное количество проведённой строки, иначе выручка (которая
    возвратом не уменьшается) и прибыль (которая уменьшалась бы) разошлись
    бы по scope - именно так и родился баг с несходящимся Прибыль=Выручка-Себестоимость."""
    user, _, lot, location = priced_sale
    sale = _complete(user, lot, unit_price="14700")
    line = sale.lines.get()
    returned = create_return(source=sale, by=user)
    add_sale_line_return(returned, line, Decimal("1"), to_location=location,
                         restock_status="available", by=user)
    complete_return(returned, by=user)
    report = get_sales_report(
        Period(timezone.localdate() - timedelta(days=1), timezone.localdate(), "")
    )
    assert report.revenue == Decimal("14700.00")
    assert report.cost == Decimal("10500.00")
    assert report.profit == Decimal("4200.00")
    assert report.profit_unavailable_lines == 0
    assert report.revenue - report.cost == report.profit


def test_formula_and_higher_customer_price_rule_are_distinct():
    from apps.brp.pricing import customer_price_rub

    assert customer_price_rub(Decimal("100"), Decimal("105"), Decimal("40")) == Decimal("14700")
    assert resolve_effective_inventory_customer_price(
        type("Inventory", (), {"receipt_customer_price_snapshot_rub": Decimal("16000")})(),
        Decimal("14700"),
    ) == Decimal("14700")


def test_owner_approved_legacy_backfill_uses_105_without_rewriting_sale_price(priced_sale):
    from django.apps import apps

    user, _, lot, _ = priced_sale
    sale = _complete(user, lot, unit_price="16000")
    line = sale.lines.get()
    line.unmarked_unit_price_rub_snapshot = None
    line.unmarked_dealer_unit_usd_snapshot = None
    line.unmarked_usd_rate_snapshot = None
    line.unmarked_price_source = ""
    line.unmarked_price_snapshot_note = ""
    line.save(update_fields=[
        "unmarked_unit_price_rub_snapshot", "unmarked_dealer_unit_usd_snapshot",
        "unmarked_usd_rate_snapshot", "unmarked_price_source", "unmarked_price_snapshot_note",
    ])
    migration = import_module("apps.sales.migrations.0007_saleline_unmarked_price_snapshot")
    migration.backfill_owner_approved_unmarked_prices(apps, None)
    line.refresh_from_db()
    assert line.unmarked_unit_price_rub_snapshot == Decimal("10500")
    assert line.unmarked_usd_rate_snapshot == Decimal("105")
    assert line.unmarked_price_snapshot_note == "legacy_reconstruction_105"
    assert line.unit_price == Decimal("16000")


def test_legacy_105_rate_snapshot_still_feeds_the_sales_report(priced_sale):
    """Регресс: 105 ₽/USD был реальным курсом на момент этих продаж и
    остаётся допустимой историческй базой - его нельзя аннулировать только
    за то, что он «105» или «фиксированный». Строка с legacy_reconstruction_105
    участвует в известном scope наравне с обычной."""
    from django.apps import apps

    user, _, lot, _ = priced_sale
    sale = _complete(user, lot, unit_price="16000")
    line = sale.lines.get()
    line.unmarked_unit_price_rub_snapshot = None
    line.unmarked_dealer_unit_usd_snapshot = None
    line.unmarked_usd_rate_snapshot = None
    line.unmarked_price_source = ""
    line.unmarked_price_snapshot_note = ""
    line.save(update_fields=[
        "unmarked_unit_price_rub_snapshot", "unmarked_dealer_unit_usd_snapshot",
        "unmarked_usd_rate_snapshot", "unmarked_price_source", "unmarked_price_snapshot_note",
    ])
    migration = import_module("apps.sales.migrations.0007_saleline_unmarked_price_snapshot")
    migration.backfill_owner_approved_unmarked_prices(apps, None)
    report = get_sales_report(
        Period(timezone.localdate() - timedelta(days=1), timezone.localdate(), "")
    )
    assert report.revenue == Decimal("16000.00")
    assert report.known_revenue == Decimal("16000.00")
    assert report.cost == Decimal("10500.00")
    assert report.profit == Decimal("5500.00")
    assert report.profit_unavailable_lines == 0


def test_impossible_aggregate_regression_profit_always_equals_revenue_minus_cost(priced_sale):
    """Регресс на конкретный сообщённый баг: Выручка=409907, Себестоимость=368066,
    Прибыль=112581 не сходятся (409907-368066=41841 != 112581). Причина была в
    том, что revenue/cost брались из замороженных Sale.revenue_total/cost_total
    (landed cost, никогда не обновлялись после возврата), а profit считался
    отдельно по дилерской базе с вычетом возвращённого количества - два разных
    scope на одном отчёте. Теперь все три числа - из одних и тех же строк и
    одной базы, поэтому равенство обязано выполняться конструктивно."""
    user, _, lot, _ = priced_sale
    _complete(user, lot, unit_price="16000", quantity="2")
    _complete(user, lot, unit_price="15000", quantity="1")

    report = get_sales_report(
        Period(timezone.localdate() - timedelta(days=1), timezone.localdate(), "")
    )

    assert report.revenue == Decimal("47000.00")
    assert report.known_revenue == Decimal("47000.00")
    assert report.cost == Decimal("31500.00")
    assert report.profit == Decimal("15500.00")
    assert report.profit_unavailable_lines == 0
    assert report.revenue - report.cost == report.profit
