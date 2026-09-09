"""Заказанные детали в таможенной выгрузке.

Заказ оформляется на ОРИГИНАЛЬНУЮ деталь, поэтому его строки идут в обычную
таможенную форму, а не в выгрузку аналогов. Но происхождение из них не
исчезает: артикул заказанной строки красится зелёным, и со строкой продажи
того же артикула она не сливается никогда.

Почему не сливается. Представьте, что ABC один раз продали со склада, а второй
раз клиент заказал её привезти. Если сложить их в одну строку, зелёная пометка
либо расползётся на проданную единицу, либо исчезнет с заказанной. Обе потери
одинаково врут сотруднику о происхождении товара, поэтому строки разные.

Предоплата в этих проверках не участвует нигде: на границе объявляют стоимость
товара по каталогу, а не сумму, которую клиент успел перевести.
"""
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from django.contrib.auth.models import Group

from apps.actions.services import (
    ORDERED_PROVENANCE,
    SALES_REPAIRS_PROVENANCE,
    customs_export_reconciliation,
    export_customs_xlsx,
    historical_customs_rows,
    perform_action,
)
from apps.brp.models import BrpCatalogPart
from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.catalog_import.models import AftermarketCatalogPart
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.ordered_parts.customs import (
    ordered_parts_customs_rows,
    ordered_parts_reconciliation,
)
from apps.ordered_parts.services import create_ordered_part, resolve_ordered_article
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.reports.services import Period, get_clients_sales_and_repairs
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from tests.customs_support import legacy_customs_completion

PASSWORD = "parol-12345"
SHEET = "Лист1"
DATA_ROW = 10
ALL_TIME = Period(None, None, "all")
GREEN = "FFC6EFCE"


@pytest.fixture
def env(db, django_user_model):
    Group.objects.all()
    admin = django_user_model.objects.create_superuser(username="boss", password=PASSWORD)
    supplier, _ = Supplier.objects.get_or_create(name="ООО Поставка")
    location, _ = StorageLocation.objects.get_or_create(
        code="S01-D01-C01",
        defaults={"name": "Ячейка", "storage_allowed": True, "is_active": True},
    )
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    return {
        "admin": admin, "sup": supplier, "loc": location,
        "cat": category, "unit": Unit.objects.get(name="Штука"),
    }


def _part(env, *, number, name="ДЕТАЛЬ", brand="BRP", price="1000"):
    manufacturer = Manufacturer.objects.get_or_create(name=brand)[0] if brand else None
    part = PartType.objects.create(
        name=name, category=env["cat"], unit=env["unit"], manufacturer=manufacturer,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal(price),
    )
    PartNumber.objects.create(
        part=part, value=number, kind=PartNumber.Kind.OEM, is_primary=True
    )
    return part


def _analog_part(env, *, number, name="АНАЛОГ"):
    part = _part(env, number=number, name=name, brand="WOODYS")
    AftermarketCatalogPart.objects.create(
        part=part, source="dealer_2023", manufacturer=part.manufacturer,
        manufacturer_number=number, source_description=name,
        dealer_cost_usd=Decimal("10.00"),
    )
    return part


def _receive(env, part, quantity="10", unit_cost="100"):
    batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
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


def _sell(env, lot, quantity="2", price="500", customer="Петров"):
    """Продажа обычным документом: снимка артикула у неё нет."""
    # Историческая продажа: карточка дозаполняется только на время
    # проведения, проверяемое происхождение строки от этого не меняется.
    with legacy_customs_completion(lot.part_type):
        sale = create_sale(customer=None, customer_name=customer, by=env["admin"])
        add_stock_lot_to_sale(
            sale, lot, Decimal(quantity), unit_price=Decimal(price), by=env["admin"]
        )
        return complete_sale(sale, by=env["admin"])


def _scanner_sell(env, part, *, quantity="2", number="", customer="Петров"):
    """Продажа сканером: артикул доказан снимком, как в реальной работе.

    Именно так столкновение артикулов и выглядит на производстве: обе строки
    названы одним номером, и только происхождение их различает.
    """
    # Историческая продажа: карточка дозаполняется только на время
    # проведения, проверяемое происхождение строки от этого не меняется.
    with legacy_customs_completion(part):
        return perform_action(
            part=part, location=env["loc"], action_type="sale", quantity=quantity,
            customer_comment=customer, scanned_number=number or "", by=env["admin"],
        )


def _customer(name="Иванов Иван"):
    return Customer.objects.create(name=name, phone="+7 912 123-45-67")


def _order(env, number, customer=None, prepayment="1000"):
    candidate, _ = resolve_ordered_article(number)
    return create_ordered_part(
        candidate=candidate, customer=customer or _customer(),
        prepayment=prepayment, by=env["admin"],
    )


def _sheet(buffer):
    return openpyxl.load_workbook(BytesIO(buffer.getvalue()))[SHEET]


def _fill(cell):
    return (cell.fill.start_color.rgb or "") if cell.fill and cell.fill.fill_type else ""


# --- 25-27. Источник, аналоги и зелёный артикул ----------------------------


def test_an_ordered_original_part_appears_in_the_normal_customs_source(env):
    _part(env, number="219800345", name="РЕМЕНЬ")
    _order(env, "219800345")

    rows = historical_customs_rows()

    ordered = [row for row in rows if row["provenance"] == ORDERED_PROVENANCE]
    assert len(ordered) == 1
    assert ordered[0]["number"] == "219800345"
    assert ordered[0]["quantity"] == Decimal("1")


def test_an_analog_part_never_reaches_the_ordered_customs_source(env):
    """Аналог вообще нельзя заказать, поэтому его строк тут не бывает."""
    from apps.ordered_parts.services import OrderedPartError

    _analog_part(env, number="SM-09374")

    with pytest.raises(OrderedPartError):
        _order(env, "SM-09374")

    assert ordered_parts_customs_rows() == []
    assert historical_customs_rows() == []


def test_the_article_cell_of_an_ordered_row_is_green(env):
    _part(env, number="219800345")
    _order(env, "219800345")

    sheet = _sheet(export_customs_xlsx(rows=historical_customs_rows()))

    assert sheet[f"B{DATA_ROW}"].value == "219800345"
    assert _fill(sheet[f"B{DATA_ROW}"]) == GREEN
    # Красится ТОЛЬКО артикул: соседние ячейки строки остаются шаблонными.
    for column in "CDEFGHJKM":
        assert _fill(sheet[f"{column}{DATA_ROW}"]) != GREEN


def test_a_normal_sale_row_article_is_not_green(env):
    part = _part(env, number="219800345")
    _receive(env, part)
    _scanner_sell(env, part, quantity="2", number="219800345")

    sheet = _sheet(export_customs_xlsx(rows=historical_customs_rows()))

    assert sheet[f"B{DATA_ROW}"].value == "219800345"
    assert _fill(sheet[f"B{DATA_ROW}"]) != GREEN


def test_green_marking_does_not_break_formulas_or_the_totals_row(env):
    _part(env, number="219800345")
    _order(env, "219800345")

    sheet = _sheet(export_customs_xlsx(rows=historical_customs_rows()))

    assert sheet[f"I{DATA_ROW}"].value == f"=J{DATA_ROW}*G{DATA_ROW}"
    assert sheet[f"L{DATA_ROW}"].value == f"=K{DATA_ROW}*J{DATA_ROW}"
    assert sheet["I150"].value == "=SUM(I7:I149)"
    assert sheet[f"B{DATA_ROW}"].border is not None


# --- 28-30. Количество и столкновение артикулов ----------------------------


def test_ordered_customs_quantity_is_one_unit_per_record(env):
    _part(env, number="219800345")
    _order(env, "219800345")

    row = [r for r in historical_customs_rows() if r["provenance"] == ORDERED_PROVENANCE][0]

    assert row["quantity"] == Decimal("1")


def test_several_ordered_records_of_one_article_keep_their_quantity(env):
    """Три заказа одного артикула складываются в одну зелёную строку из трёх."""
    _part(env, number="219800345")
    for name in ("Иванов", "Петров", "Сидоров"):
        _order(env, "219800345", _customer(name))

    ordered = [r for r in historical_customs_rows() if r["provenance"] == ORDERED_PROVENANCE]

    assert len(ordered) == 1
    assert ordered[0]["quantity"] == Decimal("3")


def test_ordered_and_sold_same_article_stay_separate_rows(env):
    """Главный случай: одна ABC продана со склада, вторая заказана клиентом."""
    part = _part(env, number="219800345")
    _receive(env, part)
    _scanner_sell(env, part, quantity="2", number="219800345")
    _order(env, "219800345")

    rows = [r for r in historical_customs_rows() if r["number"] == "219800345"]

    assert len(rows) == 2
    by_provenance = {row["provenance"]: row["quantity"] for row in rows}
    assert by_provenance == {
        SALES_REPAIRS_PROVENANCE: Decimal("2"), ORDERED_PROVENANCE: Decimal("1")
    }

    sheet = _sheet(export_customs_xlsx(rows=historical_customs_rows()))
    greens = {
        sheet[f"J{DATA_ROW + offset}"].value: _fill(sheet[f"B{DATA_ROW + offset}"])
        for offset in range(2)
    }
    # Зелёной становится ровно заказанная единица, а не весь артикул.
    assert greens[1] == GREEN
    assert greens[2] != GREEN


# --- 31-32. Цена и неполные данные -----------------------------------------


def test_the_ordered_row_uses_the_catalog_resolver_not_the_prepayment(env):
    """Предоплата 50 000 ₽ не имеет права стать таможенной ценой."""
    part = _part(env, number="420931284")
    BrpCatalogPart.objects.create(
        material_no="420931284", part_desc="DRIVE BELT",
        wholesale_price_usd=Decimal("19.63"),
    )
    _order(env, "420931284", prepayment="50000")

    row = [r for r in historical_customs_rows() if r["provenance"] == ORDERED_PROVENANCE][0]

    assert row["usd_price"] == Decimal("19.63")
    assert row["usd_price"] != Decimal("50000")
    assert row["name_en"] == "DRIVE BELT"
    assert part.pk == row["part"].pk


def test_missing_customs_fields_do_not_break_the_ordered_row(env):
    """Нет таможенной карточки: ячейки пустые, строка на месте, 500 нет."""
    _part(env, number="219800345", brand=None)
    _order(env, "219800345")

    sheet = _sheet(export_customs_xlsx(rows=historical_customs_rows()))

    assert sheet[f"B{DATA_ROW}"].value == "219800345"
    assert Decimal(str(sheet[f"J{DATA_ROW}"].value)) == Decimal("1")
    for column in "GHK":  # веса и цена не выдумываются
        assert sheet[f"{column}{DATA_ROW}"].value is None
    assert _fill(sheet[f"B{DATA_ROW}"]) == GREEN


# --- 33-35. Разделение вселенных -------------------------------------------


def test_ordered_parts_do_not_disturb_the_sales_repairs_reconciliation(env):
    """Контракт с «Продажами и ремонтами» считается без заказанных деталей."""
    part = _part(env, number="219800345")
    _sell(env, _receive(env, part), quantity="2", price="500")
    _order(env, "219800345", prepayment="7000")

    result = customs_export_reconciliation()
    report = get_clients_sales_and_repairs(ALL_TIME)
    report_quantity = sum(
        (row["sale_quantity"] + row["repair_quantity"] for row in report), Decimal("0")
    )

    assert result["totals"]["quantity"] == report_quantity == Decimal("2")
    assert result["delta"] == {"quantity": Decimal("0"), "amount": Decimal("0.00")}
    assert result["silent"] == []


def test_the_ordered_universe_reconciles_on_its_own(env):
    _part(env, number="219800345")
    _part(env, number="219800346", name="ВТОРАЯ")
    _order(env, "219800345")
    _order(env, "219800345")
    _order(env, "219800346")

    result = ordered_parts_reconciliation()

    assert result["totals"]["line_count"] == 3
    assert result["totals"]["quantity"] == Decimal("3")
    assert result["totals"]["row_quantity"] == Decimal("3")  # свёртка не теряет
    assert result["totals"]["row_count"] == 2  # два артикула
    assert result["delta_quantity"] == Decimal("0")
    assert result["silent"] == [] and result["extra"] == []


def test_the_combined_customs_universe_decomposes_by_provenance(env):
    part = _part(env, number="219800345")
    _receive(env, part)
    _scanner_sell(env, part, quantity="3", number="219800345")
    _order(env, "219800345")
    _part(env, number="219800346", name="ВТОРАЯ")
    _order(env, "219800346")

    rows = historical_customs_rows()
    sales = [r for r in rows if r["provenance"] == SALES_REPAIRS_PROVENANCE]
    ordered = [r for r in rows if r["provenance"] == ORDERED_PROVENANCE]

    assert len(sales) + len(ordered) == len(rows)  # третьей категории нет
    assert sum(r["quantity"] for r in sales) == Decimal("3")
    assert sum(r["quantity"] for r in ordered) == Decimal("2")
    # Ключи источников не пересекаются: одна строка не может числиться дважды.
    assert not {r["source_key"] for r in sales} & {r["source_key"] for r in ordered}


def test_every_row_key_is_unique_across_the_whole_export(env):
    """Защита от двойного счёта: ключ строки уникален во всей выгрузке."""
    part = _part(env, number="219800345")
    _receive(env, part)
    _scanner_sell(env, part, quantity="2", number="219800345")
    _order(env, "219800345")
    _order(env, "219800345")

    keys = [row["source_key"] for row in historical_customs_rows()]

    assert len(keys) == len(set(keys))


def test_warehouse_only_filters_exclude_ordered_rows(env):
    """У заказа нет ни ячейки, ни складского действия: под такой фильтр он не подходит."""
    part = _part(env, number="219800345")
    _sell(env, _receive(env, part), quantity="2")
    _order(env, "219800345")

    assert len(historical_customs_rows()) == 2
    by_location = historical_customs_rows(location_code="S01-D01")
    by_type = historical_customs_rows(action_type="sale")

    assert [r["provenance"] for r in by_location] == [SALES_REPAIRS_PROVENANCE]
    assert [r["provenance"] for r in by_type] == [SALES_REPAIRS_PROVENANCE]


def test_the_two_modules_agree_on_what_ordered_provenance_is_called():
    """Строка происхождения объявлена в двух модулях и обязана совпадать.

    Общий экспортёр не может импортировать раздел заказов на уровне модуля -
    получилось бы кольцо, - поэтому константа объявлена с обеих сторон. Разойдись
    они, зелёная пометка тихо перестала бы ставиться: строки были бы, а условие
    не срабатывало бы никогда.
    """
    from apps.ordered_parts.customs import PROVENANCE

    assert PROVENANCE == ORDERED_PROVENANCE
