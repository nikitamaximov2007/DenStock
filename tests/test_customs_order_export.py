"""Сохранённый заказ выгружается целиком с неизменными данными и оформлением."""

from decimal import Decimal

import openpyxl
import pytest

from apps.actions.services import TEMPLATE_PATH
from apps.brp.models import BrpCatalogPart
from apps.customs_orders.export import export_customs_order_xlsx
from apps.customs_orders.models import CustomsOrder, CustomsOrderLine
from apps.warehouse.models import ValuationSettings

pytestmark = pytest.mark.django_db


@pytest.fixture
def order():
    return CustomsOrder.objects.create(
        number=125, fx_rate=Decimal("97.1250"),
        total_quantity=Decimal("6.500"), total_rub=Decimal("37498.90"),
    )


def _line(order, source_id=1, **changes):
    values = {
        "source": CustomsOrderLine.Source.SALE,
        "source_id": source_id,
        "article": "420001234",
        "name_ru": "ПОДШИПНИК",
        "name_en": "BALL BEARING",
        "manufacturer": "BRP",
        "country": "CANADA",
        "gross_weight_kg": None,
        "net_weight_kg": None,
        "application_area": "СНЕГОХОД",
        "quantity": Decimal("2.500"),
        "wholesale_usd": Decimal("10.2500"),
        "rub_amount": Decimal("2488.83"),
    }
    values.update(changes)
    return CustomsOrderLine.objects.create(order=order, **values)


def _article_rows(sheet):
    return [row for row in range(10, sheet.max_row) if sheet[f"B{row}"].value is not None]


def test_order_export_keeps_exact_source_composition_and_analog_split(order):
    _line(order)
    _line(
        order, source=CustomsOrderLine.Source.ORDERED, is_ordered=True,
        quantity=Decimal("1.000"), rub_amount=Decimal("995.53"),
    )
    _line(
        order, 2, article="SM-01357", manufacturer="SPI", is_analog=True,
        name_ru="СТАТОР SKI-DOO", name_en="SPI STATOR SKI-DOO", country="",
        quantity=Decimal("1.000"), wholesale_usd=Decimal("203.2600"),
    )
    _line(
        order, source=CustomsOrderLine.Source.REPAIR, article="SM-09374",
        manufacturer="SPI", is_analog=True, country="", quantity=Decimal("2.000"),
        name_ru="ЩЕКА КОЛЕНЧАТОГО ВАЛА", name_en="SPI PTO CRANK WEB",
        wholesale_usd=Decimal("127.2100"),
    )
    other_order = CustomsOrder.objects.create(number=126, fx_rate=Decimal("98"))
    _line(other_order, 999, article="NOT-IN-125")

    book = openpyxl.load_workbook(export_customs_order_xlsx(order))
    assert book.sheetnames == ["Оригиналы", "Аналоги"]
    originals, analogs = book.worksheets
    assert [originals[f"B{row}"].value for row in _article_rows(originals)] == [
        "420001234", "420001234",
    ]
    assert [analogs[f"B{row}"].value for row in _article_rows(analogs)] == [
        "SM-01357", "SM-09374",
    ]
    assert originals["B10"].fill.fgColor.rgb != "FFC6EFCE"
    assert originals["B11"].fill.fgColor.rgb == "FFC6EFCE"
    assert analogs["B10"].fill.fgColor.rgb != "FFC6EFCE"
    assert analogs["B11"].fill.fgColor.rgb != "FFC6EFCE"
    assert analogs["K10"].value == 203.26
    assert analogs["K11"].value == 127.21
    exported_quantity = sum(
        Decimal(str(sheet[f"J{row}"].value))
        for sheet in book for row in _article_rows(sheet)
    )
    assert exported_quantity == Decimal("6.500")
    assert sum(len(_article_rows(sheet)) for sheet in book) == order.lines.count()


@pytest.mark.parametrize("classification", [None, False, True])
def test_order_export_always_contains_both_sheets_even_when_one_is_empty(order, classification):
    if classification is not None:
        _line(order, is_analog=classification)
    book = openpyxl.load_workbook(export_customs_order_xlsx(order))
    assert book.sheetnames == ["Оригиналы", "Аналоги"]
    assert len(_article_rows(book["Оригиналы"])) == int(classification is False)
    assert len(_article_rows(book["Аналоги"])) == int(classification is True)
    for sheet in book:
        assert sheet["I150"].value == "=SUM(I7:I149)"
        assert sheet["B12"].value is None


def test_order_export_uses_all_frozen_fields_after_catalog_and_fx_change(
    order, django_assert_num_queries,
):
    _line(
        order, gross_weight_kg=Decimal("0.456"), net_weight_kg=Decimal("0.321"),
        country="JAPAN", application_area="КВАДРОЦИКЛ",
    )
    BrpCatalogPart.objects.create(
        material_no="420001234", part_desc="CHANGED CATALOG NAME",
        wholesale_price_usd=Decimal("999.99"), retail_price_usd=Decimal("1999"),
    )
    settings = ValuationSettings.get()
    settings.current_usd_rate = Decimal("150")
    settings.save(update_fields=["current_usd_rate"])

    # Source ids need not resolve: the only data read is the saved order lines.
    with django_assert_num_queries(1):
        book = openpyxl.load_workbook(export_customs_order_xlsx(order))
    sheet = book["Оригиналы"]
    assert [sheet[f"{column}10"].value for column in "BCDEFGH"] == [
        "420001234", "ПОДШИПНИК", "BALL BEARING", "BRP", "JAPAN", 0.456, 0.321,
    ]
    assert sheet["J10"].value == 2.5
    assert sheet["K10"].value == 10.25
    assert sheet["M10"].value == "КВАДРОЦИКЛ"
    order.refresh_from_db()
    assert order.fx_rate == Decimal("97.1250")
    assert order.total_rub == Decimal("37498.90")


def test_both_sheets_keep_template_headers_dimensions_drawings_and_data_style(order):
    long_name = "КОМПЛЕКТ ОБСЛУЖИВАНИЯ СЦЕПЛЕНИЯ EDRIVE II"
    _line(order, name_ru=long_name)
    _line(order, 2, is_analog=True, name_ru=long_name)
    book = openpyxl.load_workbook(export_customs_order_xlsx(order))
    template = openpyxl.load_workbook(TEMPLATE_PATH).active
    for sheet in book:
        for row in template.iter_rows(min_row=1, max_row=9):
            for original in row:
                cell = sheet[original.coordinate]
                assert cell.value == original.value
                # This supplier template has no default style. openpyxl
                # recreates style registries on every load, so object and
                # style-id equality across workbooks are not meaningful.
                assert str(cell.font) == str(original.font)
                assert str(cell.fill) == str(original.fill)
                assert str(cell.border) == str(original.border)
                assert cell.number_format == original.number_format
        assert str(sheet.merged_cells) == str(template.merged_cells)
        assert sheet.sheet_format == template.sheet_format
        assert sheet.page_setup == template.page_setup
        assert sheet.page_margins == template.page_margins
        assert len(sheet._images) == len(template._images)
        assert {key: value.width for key, value in sheet.column_dimensions.items()} == {
            key: value.width for key, value in template.column_dimensions.items()
        }
        for column in "ABCDEFGHIJKLM":
            cell = sheet[f"{column}10"]
            assert (cell.font.name, cell.font.sz) == ("Arial", 12)
            assert cell.alignment.wrap_text is True
            # Excel's false boolean is normalized by openpyxl to None on load.
            assert cell.alignment.shrink_to_fit is not True
            assert cell.alignment.horizontal == "center"
            assert cell.alignment.vertical == "center"
        assert sheet.row_dimensions[10].height == 30
        assert sheet["F10"].value == "CANADA"
        for column in "GHI":
            assert sheet[f"{column}10"].number_format == "0.00"
        assert sheet["G10"].value is None
        assert sheet["H10"].value is None
        assert sheet["I10"].value == "=J10*G10"
        assert sheet["L10"].value == "=K10*J10"


def test_large_order_preserves_each_analog_row_and_moves_total_below_data(order):
    for source_id in range(1, 143):
        _line(order, source_id, article=f"SPI-{source_id}", is_analog=True)
    book = openpyxl.load_workbook(export_customs_order_xlsx(order))
    sheet = book["Аналоги"]
    assert len(_article_rows(sheet)) == 142
    assert sheet["B151"].value == "SPI-142"
    assert sheet["I151"].value == "=J151*G151"
    assert sheet["L151"].value == "=K151*J151"
    assert sheet["I152"].value == "=SUM(I7:I151)"
    assert "F152:H152" in sheet.merged_cells
    assert "F150:H150" not in sheet.merged_cells
    assert sheet["H150"].value is None
    assert sheet["C151"].font.sz == 12
    assert sheet["G151"].number_format == "0.00"
    assert _article_rows(book["Оригиналы"]) == []
