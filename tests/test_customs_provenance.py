"""Происхождение таможенного расхода доказывается, а не угадывается.

Две вещи, на которых выгрузка обязана стоять твёрдо: под каким номером деталь
ушла со склада и какое именно выбытие гасит возврат. Раньше обе восстанавливались
догадкой - номер брался из сегодняшней карточки, если снимка не нашлось, а
возврат списывался на последнюю незакрытую выдачу той же детали. В декларации
такая догадка становится ложным фактом.

Здесь закреплено обратное: снимок берётся из журнала действий и только оттуда,
а возврат гасит ровно свой документ.

Недоказуемый артикул выгрузку больше не отменяет: сама операция реальна и
обязана попасть в Excel, поэтому пустой остаётся ровно та ячейка, которую
нечем заполнить. Выдумать номер по сегодняшней карточке по-прежнему нельзя.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group

from apps.actions.customs_provenance import (
    ARTICLE_MISSING,
    ARTICLE_PROVEN,
    RETURN_AMBIGUOUS,
    RETURN_EXACT,
    article_snapshots,
    return_attributions,
)
from apps.actions.models import PartCustomsInfo
from apps.actions.services import (
    customs_export_reconciliation,
    historical_analog_customs_rows,
    historical_customs_rows,
    perform_action,
)
from apps.catalog.models import Category, Manufacturer, PartAnalog, PartNumber, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    create_stock_lot,
    receive_stock_lot,
    return_stock_lot_quantity,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import cancel_repair_order
from apps.returns.models import StockReturnLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.services import cancel_sale, cancel_sale_line_quantity
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
ApplicationArea = PartCustomsInfo.ApplicationArea


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="hozyain", password=PASSWORD)


@pytest.fixture
def env(db, admin):
    return {
        "admin": admin,
        "supplier": Supplier.objects.create(name="ООО Поставка"),
        "category": Category.objects.create(name="Таможня"),
        "unit": Unit.objects.get(name="Штука"),
        "loc": StorageLocation.objects.create(
            name="Ячейка", code="T-01", storage_allowed=True, is_active=True
        ),
    }


def _part(env, *, name="БОЛТ", number="219800345"):
    part = PartType.objects.create(
        name=name, category=env["category"], unit=env["unit"],
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("500"),
    )
    PartNumber.objects.create(
        part=part, value=number, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    return part


def _receive(env, part, *, quantity="20", unit_cost="100"):
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["loc"], Decimal(quantity))
    receive_stock_lot(lot, by=env["admin"])
    return lot


def _card(part, **overrides):
    """Таможенная карточка тем же путём, каким её заводит форма."""
    values = {
        "customs_name_ru": "БОЛТ", "customs_name_en": "BOLT", "manufacturer": "BRP",
        "customs_name_ru_confirmed": True,
        "country_of_origin": "CANADA", "gross_weight_kg": Decimal("0.350"),
        "net_weight_kg": Decimal("0.300"), "customs_unit_price_usd": Decimal("12.50"),
        "application_area": ApplicationArea.SNOWMOBILE,
    }
    values.update(overrides)
    card = PartCustomsInfo.objects.filter(part_type=part).first()
    if card is None:
        return PartCustomsInfo.objects.create(part_type=part, **values)
    for field, value in values.items():
        setattr(card, field, value)
    card.save()
    card.refresh_from_db()
    return card


def _sell(env, part, *, quantity="5", number="219800345"):
    return perform_action(
        part=part, location=env["loc"], action_type="sale", quantity=quantity,
        customer_comment="Иванов", scanned_number=number, by=env["admin"],
    )


def _issue(env, part, *, quantity="4", number="219800345"):
    return perform_action(
        part=part, location=env["loc"], action_type="repair", quantity=quantity,
        customer_comment="Сидоров", scanned_number=number, by=env["admin"],
    )


def _outbound():
    from apps.actions.customs_provenance import OUTBOUND_TYPES

    return list(StockMovement.objects.filter(movement_type__in=OUTBOUND_TYPES).order_by("pk"))


def _returns():
    from apps.actions.customs_provenance import RETURN_TYPES

    return list(StockMovement.objects.filter(movement_type__in=RETURN_TYPES).order_by("pk"))


def _document_sale(env, lot, *, quantity="1", price="500", customer="Петров"):
    """Продажа обычным документом - без сканера и, значит, без снимка артикула."""
    from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale

    sale = create_sale(customer=None, customer_name=customer, by=env["admin"])
    add_stock_lot_to_sale(
        sale, lot, Decimal(quantity), unit_price=Decimal(price), by=env["admin"]
    )
    return complete_sale(sale, by=env["admin"])


def _return_sale(env, action, quantity):
    """Настоящий клиентский возврат по строке продажи."""
    sale = action.sale
    document = create_return(source=sale, reason="Возврат", by=env["admin"])
    add_sale_line_return(
        document, sale.lines.get(part_type=action.part_type), Decimal(quantity),
        to_location=env["loc"], restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=env["admin"],
    )
    return complete_return(document, by=env["admin"])


# --- A-D. Историческая личность детали ------------------------------------------


def test_the_article_follows_the_snapshot_after_the_card_is_renamed(env):
    """Деталь переименовали после продажи: в выгрузке остаётся прежний номер."""
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    _sell(env, part, quantity="2", number="219800345")

    PartNumber.objects.filter(part=part).update(value="НОВЫЙ-НОМЕР")

    row = historical_customs_rows()[0]
    assert row["number"] == "219800345"
    assert row["quantity"] == Decimal("2")


def test_a_later_supersession_does_not_rewrite_history(env):
    """Замена номера по BRP приходит позже и историю не переписывает."""
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    _sell(env, part, quantity="3", number="219800345")

    # Новый номер добавлен и объявлен основным уже после выбытия.
    PartNumber.objects.filter(part=part).update(is_primary=False)
    PartNumber.objects.create(
        part=part, value="417300571", kind=PartNumber.Kind.ARTICLE, is_primary=True
    )

    assert [row["number"] for row in historical_customs_rows()] == ["219800345"]


def test_two_numbers_of_one_part_never_merge(env):
    """Один и тот же товар уходил под двумя номерами: две отдельные строки."""
    part = _part(env, number="WH-100")
    PartNumber.objects.create(part=part, value="WH-200", kind=PartNumber.Kind.ARTICLE)
    _receive(env, part)
    _card(part)
    _sell(env, part, quantity="2", number="WH-100")
    _sell(env, part, quantity="3", number="WH-200")

    rows = {row["number"]: row["quantity"] for row in historical_customs_rows()}
    assert rows == {"WH-100": Decimal("2"), "WH-200": Decimal("3")}


def test_an_alias_does_not_replace_the_number_the_goods_left_under(env):
    part = _part(env, number="UCP-OLD")
    _receive(env, part)
    _card(part)
    _sell(env, part, quantity="2", number="UCP-OLD")

    PartNumber.objects.create(
        part=part, value="USE-NEW", kind=PartNumber.Kind.ANALOG, is_primary=False
    )

    assert [row["number"] for row in historical_customs_rows()] == ["UCP-OLD"]


def test_an_outbound_without_a_snapshot_is_never_named_by_the_current_card(env):
    """Продажа не сканером: строка уходит в Excel, но номер остаётся пустым.

    Снимка артикула у такого документа нет, а сегодняшний номер карточки
    историей не является. Раньше выбор был между выдумкой и потерей строки;
    теперь операция сохраняется, а недоказанное поле остаётся пустым.
    """
    part = _part(env, number="219800345")
    _card(part)
    lot = _receive(env, part)
    _document_sale(env, lot, quantity="2", price="500")

    rows = historical_customs_rows()
    assert len(rows) == 1
    assert rows[0]["number"] == ""  # не «219800345» из сегодняшней карточки
    assert rows[0]["quantity"] == Decimal("2")
    result = customs_export_reconciliation()
    assert len(result["article_unproven"]) == 1
    assert result["silent"] == []


def test_a_write_off_is_outside_the_customs_universe(env):
    """Списание клиенту не уходило: в «Продажах и ремонтах» его нет, и здесь нет."""
    from apps.inventory.services import write_off_stock_lot_quantity

    part = _part(env, number="219800345")
    _card(part)
    lot = _receive(env, part)
    write_off_stock_lot_quantity(lot, Decimal("2"), by=env["admin"], comment="Брак")

    resolved = article_snapshots(_outbound())
    assert [entry["status"] for entry in resolved.values()] == [ARTICLE_MISSING]
    assert historical_customs_rows() == []
    assert customs_export_reconciliation()["lines"] == []


def test_a_proven_snapshot_names_its_source(env):
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    _sell(env, part, quantity="2", number="219800345")

    resolved = article_snapshots(_outbound())
    entry = next(iter(resolved.values()))
    assert entry["status"] == ARTICLE_PROVEN
    assert entry["number"] == "219800345"
    assert entry["source"] == "action"


# --- Возврат гасит своё выбытие --------------------------------------------------


def test_a_return_reduces_only_its_own_document(env):
    """Одна деталь, один лот, продажа и ремонт: возврат из продажи трогает продажу."""
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    sale = _sell(env, part, quantity="2", number="219800345")
    _issue(env, part, quantity="2", number="219800345")

    _return_sale(env, sale, "1")

    result = customs_export_reconciliation()
    by_kind = {}
    for record in result["lines"]:
        by_kind[record["kind"]] = by_kind.get(record["kind"], Decimal("0")) + (
            record["quantity"]
        )
    assert by_kind == {"sale": Decimal("1"), "repair": Decimal("2")}
    assert result["silent"] == []


def test_a_bare_compensating_movement_never_reduces_a_documented_sale(env):
    """Движение без документа возврата не гасит продажу - ни здесь, ни в отчёте.

    Так выглядят исторические движения отмены: тип документа не проставлен, а
    номер - продажи. Доказать по нему нечего, и «Продажи и ремонты» его тоже не
    видят: проведённая продажа остаётся проведённой на все 4. Трактовать такое
    движение отдельным правилом ради Excel нельзя - итоги разошлись бы.
    """
    from apps.reports.services import Period, get_clients_sales_and_repairs

    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    sale = _sell(env, part, quantity="4", number="219800345")

    return_stock_lot_quantity(
        StockLot.objects.first().batch_line, env["loc"], Decimal("1"),
        unit_cost_rub=Decimal("100"), restock_status=StockLot.Status.AVAILABLE,
        by=env["admin"], document_id=sale.sale_id, comment="Отмена продажи",
    )

    attribution = next(iter(return_attributions(_returns()).values()))
    assert attribution["status"] == RETURN_AMBIGUOUS  # доказательства и правда нет
    result = customs_export_reconciliation()
    assert sum(r["quantity"] for r in result["lines"]) == Decimal("4")
    report = get_clients_sales_and_repairs(Period(None, None, "all"))
    assert sum(row["sale_quantity"] for row in report) == Decimal("4")
    assert result["delta"] == {"quantity": Decimal("0"), "amount": Decimal("0.00")}


def test_two_sales_of_one_lot_keep_their_own_returns(env):
    """Продажа 2 и продажа 3 из одного лота: возврат знает свою."""
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    first = _sell(env, part, quantity="2", number="219800345")
    _sell(env, part, quantity="3", number="219800345")

    _return_sale(env, first, "1")

    result = customs_export_reconciliation()
    quantities = sorted(record["quantity"] for record in result["lines"])
    assert quantities == [Decimal("1"), Decimal("3")]
    assert result["silent"] == []


def test_a_completed_return_document_proves_its_source(env):
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    action = _sell(env, part, quantity="4", number="219800345")
    sale = action.sale
    document = create_return(source=sale, reason="Не подошло", by=env["admin"])
    add_sale_line_return(
        document, sale.lines.get(), Decimal("1"),
        to_location=env["loc"], restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=env["admin"],
    )
    complete_return(document, by=env["admin"])

    attribution = next(iter(return_attributions(_returns()).values()))
    assert attribution["status"] == RETURN_EXACT
    assert attribution["proof"] == "return_line"
    assert sum(
        record["quantity"] for record in customs_export_reconciliation()["lines"]
    ) == Decimal("3")


# --- Отмены, введённые предыдущими выпусками ---------------------------------------


def test_a_partial_line_cancellation_keeps_exact_provenance(env):
    """Продано 4, отменена 1: расход становится 3, догадок нет."""
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    action = _sell(env, part, quantity="4", number="219800345")

    cancel_sale_line_quantity(
        action.sale.lines.get(), 1, reason="Клиент вернул", author="Иванов И.",
        by=env["admin"],
    )

    result = customs_export_reconciliation()
    assert sum(record["quantity"] for record in result["lines"]) == Decimal("3")
    assert result["silent"] == []
    assert result["article_unproven"] == []


def test_a_whole_cancellation_after_a_partial_one_reconciles_all_four(env):
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    action = _sell(env, part, quantity="4", number="219800345")
    cancel_sale_line_quantity(
        action.sale.lines.get(), 1, reason="Одна", author="И.", by=env["admin"]
    )

    cancel_sale(action.sale, by=env["admin"], reason="Остальное", author="Иванов И.")

    result = customs_export_reconciliation()
    assert sum(record["quantity"] for record in result["lines"]) == Decimal("0")
    assert historical_customs_rows() == []  # нулевой расход строки не образует
    assert result["delta"] == {"quantity": Decimal("0"), "amount": Decimal("0.00")}


def test_a_repair_cancellation_reconciles_its_own_issue(env):
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    _sell(env, part, quantity="2", number="219800345")
    repair = _issue(env, part, quantity="3", number="219800345")

    cancel_repair_order(
        repair.repair_order, by=env["admin"], reason="Отменён", author="Иванов И."
    )

    result = customs_export_reconciliation()
    assert sum(record["quantity"] for record in result["lines"]) == Decimal("2")
    assert {record["kind"] for record in result["lines"]} == {"sale"}
    assert result["delta"] == {"quantity": Decimal("0"), "amount": Decimal("0.00")}


# --- Итоговый инвариант и выгрузка ---------------------------------------------------


def test_every_canonical_line_lands_in_the_export(env):
    """Молчаливой категории не существует: строка либо в XLSX, либо её нет вовсе."""
    from apps.inventory.services import write_off_stock_lot_quantity

    part = _part(env, number="219800345")
    lot = _receive(env, part)
    _card(part)
    _sell(env, part, quantity="4", number="219800345")
    write_off_stock_lot_quantity(lot, Decimal("2"), by=env["admin"], comment="Брак")
    naked = _part(env, name="БЕЗ ДАННЫХ", number="777000777")
    naked_lot = _receive(env, naked)
    _sell(env, naked, quantity="1", number="777000777")
    _document_sale(env, naked_lot, quantity="2", price="300")

    result = customs_export_reconciliation()
    assert result["silent"] == []
    assert result["duplicates"] == []
    # 4 продажи + 1 продажа детали без карточки + 2 продажи документом.
    assert result["totals"]["quantity"] == Decimal("7")
    assert result["totals"]["row_quantity"] == Decimal("7")  # свёртка ничего не теряет
    assert len(result["incomplete"]) == 2  # обе строки детали без карточки
    assert len(result["article_unproven"]) == 1  # продажа документом
    assert result["delta"] == {"quantity": Decimal("0"), "amount": Decimal("0.00")}


def test_the_xlsx_is_produced_even_when_the_article_is_unproven(client, env, admin):
    from django.urls import reverse

    part = _part(env, number="219800345")
    _card(part)
    lot = _receive(env, part)
    _document_sale(env, lot, quantity="4", price="500")
    client.force_login(admin)

    response = client.get(reverse("actions_export"))

    assert response.status_code == 200
    assert response["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )


def test_the_xlsx_is_produced_when_everything_is_proven(client, env, admin):
    from django.urls import reverse

    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    _sell(env, part, quantity="2", number="219800345")
    client.force_login(admin)

    response = client.get(reverse("actions_export"))

    assert response.status_code == 200
    assert response["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )


def test_the_preview_and_the_file_stand_on_the_same_rows(env):
    part = _part(env, number="219800345")
    _receive(env, part)
    _card(part)
    _sell(env, part, quantity="2", number="219800345")

    rows = historical_customs_rows()
    result = customs_export_reconciliation()
    assert len(result["lines"]) == 1
    assert sum(record["quantity"] for record in result["lines"]) == rows[0]["quantity"]
    assert result["lines"][0]["number"] == rows[0]["number"]
    assert result["rows"] == rows


def test_partanalog_direction_splits_original_and_analog_exports(env):
    original = _part(env, name="ОРИГИНАЛ", number="ORIG-1")
    analog = _part(env, name="АНАЛОГ", number="ALT-1")
    PartAnalog.objects.create(original=original, analog=analog)
    _receive(env, original, quantity="3")
    _receive(env, analog, quantity="4")
    _sell(env, original, quantity="2", number="ORIG-1")
    _sell(env, analog, quantity="3", number="ALT-1")

    normal = historical_customs_rows()
    analog_rows = historical_analog_customs_rows()

    assert {row["source_key"][0] for row in normal} == {original.pk}
    assert {row["source_key"][0] for row in analog_rows} == {analog.pk}
    normal_parts = {row["source_key"][0] for row in normal}
    analog_parts = {row["source_key"][0] for row in analog_rows}
    assert not normal_parts & analog_parts
    assert sum((row["quantity"] for row in normal + analog_rows), Decimal("0")) == Decimal("5")


def test_spi_manufacturer_is_analog_only(env):
    spi = Manufacturer.objects.create(name="SPI")
    part = _part(env, name="SPI STATOR", number="SM-01357")
    part.manufacturer = spi
    part.save(update_fields=["manufacturer"])
    _receive(env, part, quantity="1")
    _sell(env, part, quantity="1", number="SM-01357")

    assert historical_customs_rows() == []
    rows = historical_analog_customs_rows()
    assert len(rows) == 1
    assert rows[0]["number"] == "SM-01357"
