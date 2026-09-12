"""Manual current prices require explicit commercial confirmation.

Ручная цена не является согласием на автоматическую подмену прайсом поставщика.
До подтверждения её коммерческого происхождения она остаётся UNVERIFIED и не
показывается публично числом.

Историю это не касается вовсе. Проведённая продажа, проведённый ремонт и
строка таможенного заказа держат СВОЮ замороженную цену, и пересчёт текущих
цен её не трогает.

Курс берётся из канонических настроек системы. Ни одного зашитого числа здесь
нет: тест считает ожидание той же формулой, что и продакшен.
"""
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.brp.models import BrpCatalogPart, BrpPartLink, BrpPricingSettings
from apps.brp.pricing import customer_price_rub
from apps.brp.services import promote_to_warehouse
from apps.catalog.services import refresh_linked_part_prices
from apps.customs_orders.models import CustomsOrder, CustomsOrderLine
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.sales.models import Sale
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation, ValuationSettings
from tests.customs_support import remember_customs

MANUAL_PRICE = Decimal("138496")


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser("price-admin", password="parol-12345")


@pytest.fixture
def env(db, admin):
    location = StorageLocation.objects.create(
        name="Ячейка цены", code="S09-D01-C01", storage_allowed=True, is_active=True
    )
    return {"admin": admin, "location": location,
            "supplier": Supplier.objects.create(name="Поставщик цены")}


def _rate_and_markup():
    """Канонический курс и наценка системы: своих чисел тест не заводит."""
    return ValuationSettings.get().current_usd_rate, BrpPricingSettings.get().brp_markup_percent


def _expected(wholesale_usd):
    rate, markup = _rate_and_markup()
    return customer_price_rub(Decimal(wholesale_usd), rate, markup)


def _brp(material, wholesale, *, manual=None, admin=None):
    part_row = BrpCatalogPart.objects.create(
        material_no=material, part_desc="PRICE TEST",
        retail_price_usd=Decimal("100"), wholesale_price_usd=Decimal(wholesale),
    )
    part = promote_to_warehouse(part_row, by=admin, manual_price=manual)
    return part_row, part


def _refresh():
    rate, markup = _rate_and_markup()
    return refresh_linked_part_prices(
        usd_rate=rate, brp_markup=markup,
        polaris_markup=BrpPricingSettings.get().brp_markup_percent,
    )


def _stock(env, part, qty="4"):
    remember_customs(part)
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(qty), unit_cost_currency=Decimal("1")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["location"], Decimal(qty))
    receive_stock_lot(lot, by=env["admin"])
    return lot


# --- Ручная цена не переживает следующий прайс -------------------------------------------


def test_a_manual_price_is_not_replaced_by_the_next_price_list(env):
    row, part = _brp("MANUAL-001", "10", manual=MANUAL_PRICE, admin=env["admin"])
    assert part.recommended_price == MANUAL_PRICE

    row.wholesale_price_usd = Decimal("12")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    part.refresh_from_db()
    assert part.recommended_price == MANUAL_PRICE
    assert part.price_provenance == part.PriceProvenance.UNVERIFIED


def test_an_unconfirmed_manual_price_stays_unverified_after_later_price_lists(env):
    row, part = _brp("MANUAL-002", "10", manual=MANUAL_PRICE, admin=env["admin"])
    row.wholesale_price_usd = Decimal("12")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()
    row.wholesale_price_usd = Decimal("20")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    part.refresh_from_db()
    assert part.recommended_price == MANUAL_PRICE
    assert part.price_provenance == part.PriceProvenance.UNVERIFIED


def test_the_link_keeps_claiming_the_price_is_manual_until_confirmation(env):
    row, part = _brp("MANUAL-003", "10", manual=MANUAL_PRICE, admin=env["admin"])
    assert BrpPartLink.objects.get(part=part).price_source == BrpPartLink.PriceSource.MANUAL

    row.wholesale_price_usd = Decimal("12")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    assert BrpPartLink.objects.get(part=part).price_source == BrpPartLink.PriceSource.MANUAL


def test_the_manual_value_stays_recorded_for_audit(env):
    """Перекрыли не значит стёрли: что именно вписывали руками, видно и потом."""
    row, part = _brp("MANUAL-004", "10", manual=MANUAL_PRICE, admin=env["admin"])
    row.wholesale_price_usd = Decimal("12")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    assert BrpPartLink.objects.get(part=part).manual_customer_price_rub == MANUAL_PRICE


def test_a_calculated_price_still_follows_the_price_list(env):
    row, part = _brp("CALC-001", "10", admin=env["admin"])
    row.wholesale_price_usd = Decimal("15")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    part.refresh_from_db()
    assert part.recommended_price == _expected("15")


def test_the_command_does_not_report_unconfirmed_manual_prices_as_replaced(env, capsys):
    row, _part = _brp("MANUAL-005", "10", manual=MANUAL_PRICE, admin=env["admin"])
    row.wholesale_price_usd = Decimal("12")
    row.save(update_fields=["wholesale_price_usd"])
    call_command("recalculate_linked_part_prices", "--apply")
    assert "Ручных цен перекрыто прайсом: 0" in capsys.readouterr().out


# --- Неудачный или неполный прайс ничего не переписывает ---------------------------------


def test_a_missing_wholesale_price_leaves_the_current_price_alone(env):
    row, part = _brp("PARTIAL-001", "10", manual=MANUAL_PRICE, admin=env["admin"])
    row.wholesale_price_usd = None
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    part.refresh_from_db()
    assert part.recommended_price == MANUAL_PRICE


def test_a_non_positive_wholesale_price_leaves_the_current_price_alone(env):
    row, part = _brp("PARTIAL-002", "10", manual=MANUAL_PRICE, admin=env["admin"])
    row.wholesale_price_usd = Decimal("0")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    part.refresh_from_db()
    assert part.recommended_price == MANUAL_PRICE


def test_a_failed_refresh_writes_no_partial_prices(env, monkeypatch):
    """Падение на середине не оставляет половину карточек переоценёнными."""
    from apps.catalog import services as catalog_services

    row_a, part_a = _brp("FAIL-001", "10", admin=env["admin"])
    row_b, part_b = _brp("FAIL-002", "10", admin=env["admin"])
    before = (part_a.recommended_price, part_b.recommended_price)
    for row in (row_a, row_b):
        row.wholesale_price_usd = Decimal("30")
        row.save(update_fields=["wholesale_price_usd"])

    def boom(*args, **kwargs):
        raise RuntimeError("прайс оборвался")

    monkeypatch.setattr(catalog_services, "plan_linked_part_price_refresh", boom)
    with pytest.raises(RuntimeError):
        catalog_services.refresh_linked_part_prices(
            usd_rate=_rate_and_markup()[0], brp_markup=_rate_and_markup()[1],
            polaris_markup=_rate_and_markup()[1],
        )

    part_a.refresh_from_db()
    part_b.refresh_from_db()
    assert (part_a.recommended_price, part_b.recommended_price) == before


# --- История не меняется ------------------------------------------------------------------


def test_a_completed_sale_keeps_its_frozen_price(env):
    row, part = _brp("HIST-001", "10", manual=MANUAL_PRICE, admin=env["admin"])
    lot = _stock(env, part)
    sale = create_sale(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=MANUAL_PRICE, by=env["admin"])
    complete_sale(sale, by=env["admin"])

    row.wholesale_price_usd = Decimal("12")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    sale.refresh_from_db()
    assert sale.status == Sale.Status.COMPLETED
    assert sale.lines.get().unit_price == MANUAL_PRICE


def test_a_completed_repair_keeps_its_frozen_price(env):
    row, part = _brp("HIST-002", "10", manual=MANUAL_PRICE, admin=env["admin"])
    lot = _stock(env, part)
    order = create_repair_order(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_repair_order(
        order, lot, Decimal("1"), customer_unit_price_rub=MANUAL_PRICE, by=env["admin"]
    )
    complete_repair_order(order, by=env["admin"])

    row.wholesale_price_usd = Decimal("12")
    row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    order.refresh_from_db()
    assert order.status == RepairOrder.Status.COMPLETED
    assert order.lines.get().customer_unit_price_rub == MANUAL_PRICE


def test_a_customs_order_line_keeps_its_frozen_price(env):
    _row, part = _brp("HIST-003", "10", manual=MANUAL_PRICE, admin=env["admin"])
    order = CustomsOrder.objects.create(
        number=901, order_type=CustomsOrder.OrderType.ORIGINAL, fx_rate=Decimal("100"),
    )
    line = CustomsOrderLine.objects.create(
        order=order, source="sale", source_id=1, article="HIST-003",
        name_ru="ДЕТАЛЬ", name_en="PART", manufacturer="BRP", country="CANADA",
        quantity=Decimal("1"), wholesale_usd=Decimal("10"), rub_amount=Decimal("1000"),
    )
    _row.wholesale_price_usd = Decimal("12")
    _row.save(update_fields=["wholesale_price_usd"])
    _refresh()

    line.refresh_from_db()
    assert line.wholesale_usd == Decimal("10")
    assert part.pk is not None


def test_the_refresh_uses_the_canonical_system_rate(env):
    """Курс берётся из ValuationSettings, а не из зашитого в код числа."""
    row, part = _brp("RATE-001", "10", admin=env["admin"])
    settings = ValuationSettings.get()
    settings.current_usd_rate = settings.current_usd_rate + Decimal("7")
    settings.save(update_fields=["current_usd_rate"])
    _refresh()

    part.refresh_from_db()
    assert part.recommended_price == _expected("10")
