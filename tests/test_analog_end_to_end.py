"""Аналог должен вести себя как обычная складская деталь - всюду.

Смысл связи не в самой связи, а в том, что деталью потом работают: принимают,
продают, выдают в ремонт, возвращают, считают. И везде документы обязаны
хранить именно ту деталь, которую держали в руках, а не исходную.

Подмена аналитики здесь была бы самым дорогим из возможных дефектов: отчёт
показывал бы, что продали оригинал, которого на складе не было ни дня.
"""
from decimal import Decimal

import pytest

from apps.catalog.models import PartAnalog
from apps.catalog.services import create_manual_part, link_analog, unlink_analog
from apps.core.part_lookup import resolve_part_lookup
from apps.customers.models import Customer
from apps.inventory.models import StockBalance
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import get_client_part_history, resolve_period
from apps.returns.models import StockReturnLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
SAME = "420123456"


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="boss", password=PASSWORD)


@pytest.fixture
def scene(db, admin):
    """Оригинала нет, аналог лежит на полке в количестве четырёх штук."""
    original = create_manual_part(
        name="Поршень BRP", article=SAME, price=Decimal("10000"), manufacturer_name="BRP"
    )
    analog = create_manual_part(
        name="Поршень XYZ", article=SAME, price=Decimal("4500"), manufacturer_name="XYZ"
    )
    link_analog(original=original, analog=analog, by=admin)
    shelf = StorageLocation.objects.create(
        name="Ячейка", code="S02-D03-C01", storage_allowed=True, is_active=True
    )
    lot = _receive(analog, admin, shelf, quantity="4", unit_cost="2600")
    return {"admin": admin, "original": original, "analog": analog,
            "shelf": shelf, "lot": lot}


def _receive(part, admin, where, *, quantity, unit_cost):
    supplier, _ = Supplier.objects.get_or_create(name="ООО Поставка")
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, where, Decimal(quantity))
    receive_stock_lot(lot, by=admin)
    return lot


def _available(part, where):
    balance = StockBalance.objects.filter(part_type=part, location=where).first()
    return balance.quantity_available if balance else Decimal("0")


# --- Приёмка ---------------------------------------------------------------------


def test_receiving_puts_the_stock_on_the_analog_only(scene):
    assert _available(scene["analog"], scene["shelf"]) == Decimal("4")
    assert _available(scene["original"], scene["shelf"]) == Decimal("0")


def test_the_incoming_cost_belongs_to_the_lot_not_to_the_card(scene):
    """Цена в карточке - продажная. Себестоимость появляется на приёмке."""
    assert scene["lot"].landed_unit_cost_rub == Decimal("2600.00")
    assert scene["analog"].recommended_price == Decimal("4500")


def test_the_analog_keeps_its_own_lots_and_cells(scene):
    other = StorageLocation.objects.create(
        name="Вторая", code="S04-D04-C02", storage_allowed=True, is_active=True
    )
    _receive(scene["analog"], scene["admin"], other, quantity="3", unit_cost="2900")

    assert _available(scene["analog"], scene["shelf"]) == Decimal("4")
    assert _available(scene["analog"], other) == Decimal("3")
    assert _available(scene["original"], other) == Decimal("0")


# --- Поиск -----------------------------------------------------------------------


def test_one_article_finds_both_parts(scene):
    """Тот самый случай: Денис ищет артикул и должен увидеть обе карточки."""
    found = resolve_part_lookup(SAME, allow_partial=True, allow_name=True)
    names = {candidate.part.name for candidate in found.candidates}
    assert names == {"Поршень BRP", "Поршень XYZ"}


def test_the_search_says_which_one_is_the_analog(scene):
    found = resolve_part_lookup(SAME, allow_partial=True, allow_name=True)
    by_name = {c.part.name: c for c in found.candidates}
    assert by_name["Поршень XYZ"].analog_for == ["Поршень BRP"]
    assert by_name["Поршень BRP"].analog_for == []


def test_the_search_shows_the_real_availability_of_each(scene):
    found = resolve_part_lookup(SAME, allow_partial=True, allow_name=True, include_price=True)
    by_name = {c.part.name: c for c in found.candidates}
    assert by_name["Поршень BRP"].available == Decimal("0")
    assert by_name["Поршень XYZ"].available == Decimal("4")
    assert by_name["Поршень XYZ"].client_price == Decimal("4500")


@pytest.mark.parametrize(
    "typed", ["420123456", "420-123-456", "420 123 456", " 420123456 ", "420.123.456"]
)
def test_the_article_is_found_however_it_is_written(scene, typed):
    found = resolve_part_lookup(typed, allow_partial=True, allow_name=True)
    assert len(found.candidates) == 2


def test_the_analog_is_found_by_its_own_name(scene):
    found = resolve_part_lookup("XYZ", allow_partial=True, allow_name=True)
    assert [c.part.name for c in found.candidates] == ["Поршень XYZ"]


# --- Продажа ---------------------------------------------------------------------


def test_selling_an_analog_moves_only_its_own_stock(scene):
    sale = create_sale(customer=None, customer_name="Иванов", by=scene["admin"])
    add_stock_lot_to_sale(
        sale, scene["lot"], Decimal("2"), unit_price=Decimal("4500"), by=scene["admin"]
    )
    sale = complete_sale(sale, by=scene["admin"])

    assert _available(scene["analog"], scene["shelf"]) == Decimal("2")
    assert _available(scene["original"], scene["shelf"]) == Decimal("0")
    assert sale.lines.get().part_type_id == scene["analog"].pk


def test_the_sale_document_never_records_the_original_instead(scene):
    """Подмена здесь означала бы, что отчёт врёт про то, что продали."""
    sale = create_sale(customer=None, customer_name="Иванов", by=scene["admin"])
    add_stock_lot_to_sale(
        sale, scene["lot"], Decimal("1"), unit_price=Decimal("4500"), by=scene["admin"]
    )
    sale = complete_sale(sale, by=scene["admin"])

    line = sale.lines.get()
    assert line.part_type.name == "Поршень XYZ"
    assert line.part_type_id != scene["original"].pk


def test_a_return_puts_the_analog_back_on_its_own_shelf(scene):
    sale = create_sale(customer=None, customer_name="Иванов", by=scene["admin"])
    add_stock_lot_to_sale(
        sale, scene["lot"], Decimal("3"), unit_price=Decimal("4500"), by=scene["admin"]
    )
    sale = complete_sale(sale, by=scene["admin"])
    assert _available(scene["analog"], scene["shelf"]) == Decimal("1")

    ret = create_return(source=sale, by=scene["admin"])
    add_sale_line_return(
        ret, sale.lines.get(), Decimal("3"),
        to_location=scene["shelf"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=scene["admin"],
    )
    complete_return(ret, by=scene["admin"])

    assert _available(scene["analog"], scene["shelf"]) == Decimal("4")
    assert _available(scene["original"], scene["shelf"]) == Decimal("0")


# --- Ремонт ----------------------------------------------------------------------


def test_issuing_an_analog_into_repair_records_the_analog(scene):
    customer = Customer.objects.create(name="Иванов")
    order = create_repair_order(customer=customer, customer_name="", by=scene["admin"])
    add_stock_lot_to_repair_order(order, scene["lot"], Decimal("2"), by=scene["admin"])
    order = complete_repair_order(order, by=scene["admin"])

    line = order.lines.get()
    assert line.part_type_id == scene["analog"].pk
    assert line.unit_cost_rub == Decimal("2600.00")
    assert line.total_cost_rub == Decimal("5200.00")
    assert _available(scene["analog"], scene["shelf"]) == Decimal("2")


def test_the_repair_cost_comes_from_the_analog_lot_not_the_original_price(scene):
    """Себестоимость - это то, во что обошёлся именно выданный лот."""
    customer = Customer.objects.create(name="Иванов")
    order = create_repair_order(customer=customer, customer_name="", by=scene["admin"])
    add_stock_lot_to_repair_order(order, scene["lot"], Decimal("1"), by=scene["admin"])
    order = complete_repair_order(order, by=scene["admin"])

    line = order.lines.get()
    assert line.unit_cost_rub != scene["original"].recommended_price
    assert line.unit_cost_rub != scene["analog"].recommended_price


# --- Отчёты и история клиента ------------------------------------------------------


def test_the_client_history_shows_the_part_that_was_actually_used(scene):
    customer = Customer.objects.create(name="Иванов")
    sale = create_sale(customer=customer, customer_name="", by=scene["admin"])
    add_stock_lot_to_sale(
        sale, scene["lot"], Decimal("1"), unit_price=Decimal("4500"), by=scene["admin"]
    )
    complete_sale(sale, by=scene["admin"])

    order = create_repair_order(customer=customer, customer_name="", by=scene["admin"])
    add_stock_lot_to_repair_order(order, scene["lot"], Decimal("1"), by=scene["admin"])
    complete_repair_order(order, by=scene["admin"])

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert {row["part_name"] for row in rows} == {"Поршень XYZ"}
    assert all(row["part_name"] != "Поршень BRP" for row in rows)


def test_the_two_money_values_stay_in_their_own_columns_for_an_analog(scene):
    customer = Customer.objects.create(name="Иванов")
    sale = create_sale(customer=customer, customer_name="", by=scene["admin"])
    add_stock_lot_to_sale(
        sale, scene["lot"], Decimal("1"), unit_price=Decimal("4500"), by=scene["admin"]
    )
    complete_sale(sale, by=scene["admin"])
    order = create_repair_order(customer=customer, customer_name="", by=scene["admin"])
    add_stock_lot_to_repair_order(order, scene["lot"], Decimal("1"), by=scene["admin"])
    complete_repair_order(order, by=scene["admin"])

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    for row in rows:
        assert (row["amount"] is None) != (row["cost"] is None)


def test_history_survives_the_link_being_removed(scene):
    """Связь - это мнение о деталях. Документы её не спрашивают."""
    customer = Customer.objects.create(name="Иванов")
    sale = create_sale(customer=customer, customer_name="", by=scene["admin"])
    add_stock_lot_to_sale(
        sale, scene["lot"], Decimal("1"), unit_price=Decimal("4500"), by=scene["admin"]
    )
    complete_sale(sale, by=scene["admin"])

    unlink_analog(PartAnalog.objects.get())

    rows = get_client_part_history(resolve_period({}), customer_id=customer.pk)
    assert [row["part_name"] for row in rows] == ["Поршень XYZ"]
    assert sale.lines.get().part_type_id == scene["analog"].pk


# --- Выбор детали в документах -----------------------------------------------------


def test_the_option_label_tells_the_two_apart(scene):
    """При одинаковом артикуле различает производитель, и он уже в подписи."""
    from apps.inventory.presentation import part_option_label

    original = part_option_label(scene["original"])
    analog = part_option_label(scene["analog"])

    assert original != analog, "две детали с одним артикулом неразличимы в списке"
    assert SAME in original and SAME in analog
    assert "BRP" in original
    assert "XYZ" in analog


def test_the_lot_label_adds_quantity_and_cell(scene):
    """У продажи и ремонта выбирают лот, и там видно ещё количество и ячейку."""
    from apps.inventory.presentation import lot_option_label

    label = lot_option_label(scene["lot"])
    assert "Поршень XYZ" in label
    assert "XYZ" in label
    assert "4" in label
    assert "S02-D03-C01" in label


def test_the_receipt_selector_offers_both_parts(scene):
    """Одинаковый артикул не должен делать выбор невозможным."""
    from apps.receipts.forms import ReceiptLineForm

    form = ReceiptLineForm()
    labels = [str(label) for _, label in form.fields["part_type"].choices if label]
    assert any("Поршень BRP" in label for label in labels)
    assert any("Поршень XYZ" in label for label in labels)


def test_the_sale_selector_offers_the_analog_lot(scene):
    from apps.sales.forms import AddSaleLotForm

    form = AddSaleLotForm()
    labels = [str(label) for _, label in form.fields["lot"].choices if label]
    assert any("Поршень XYZ" in label for label in labels)
    assert any("S02-D03-C01" in label for label in labels), "не видно, из какой ячейки"


def test_the_repair_selector_offers_the_analog_lot(scene):
    from apps.repairs.forms import AddRepairLotForm

    form = AddRepairLotForm()
    labels = [str(label) for _, label in form.fields["lot"].choices if label]
    assert any("Поршень XYZ" in label for label in labels)


# --- Инвентаризация ----------------------------------------------------------------


def test_the_analog_is_counted_on_its_own(scene):
    """Остатки не складываются с исходной деталью ни в каком виде."""
    from apps.inventory.movement import live_stock_rows

    rows = live_stock_rows(part_ids=[scene["original"].pk, scene["analog"].pk])
    by_part = {}
    for row in rows:
        by_part.setdefault(row.part_type.pk, Decimal("0"))
        by_part[row.part_type.pk] += row.available

    assert by_part.get(scene["analog"].pk) == Decimal("4")
    assert by_part.get(scene["original"].pk) is None
