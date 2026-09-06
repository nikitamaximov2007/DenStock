"""Каталожное авто-заполнение таможенного экспорта по exact-артикулу.

Утверждённый контракт: сохранённый ввод оператора имеет приоритет; пустые
поля заполняются из загруженных каталогов поставщика; трекинг, веса, область
применения и страна не-BRP брендов остаются пустыми для ручного ввода.
"""

from decimal import Decimal

import openpyxl
import pytest
from django.utils import timezone

from apps.actions.models import PartCustomsDataVersion, PartCustomsInfo
from apps.actions.services import _customs_row_from_version, export_customs_xlsx
from apps.brp.models import BrpCatalogPart
from apps.catalog.models import Category, Manufacturer, PartType, Unit
from apps.catalog_import.models import AftermarketCatalogPart

pytestmark = pytest.mark.django_db


@pytest.fixture
def part_factory():
    category = Category.objects.create(name="Auto-fill test")
    unit = Unit.objects.get(name="Штука")

    def create(brand=None):
        manufacturer = Manufacturer.objects.get_or_create(name=brand)[0] if brand else None
        return PartType.objects.create(
            name="Auto-fill test", category=category, unit=unit, manufacturer=manufacturer
        )

    return create


def _aftermarket(part, number="SM-01357", desc="SPI STATOR SKI DOO", usd="203.26"):
    return AftermarketCatalogPart.objects.create(
        part=part, source="dealer_2023", manufacturer=part.manufacturer,
        manufacturer_number=number, source_description=desc,
        dealer_cost_usd=Decimal(usd),
    )


def test_brp_article_lookup_fills_fields_without_link(part_factory):
    part = part_factory()  # ни производителя, ни BrpPartLink: только артикул
    BrpCatalogPart.objects.create(
        material_no="420931284", part_desc="DRIVE BELT",
        wholesale_price_usd=Decimal("19.63"),
    )
    row = _customs_row_from_version(part, None, Decimal("2"), number="420931284")
    assert row["number"] == "420931284"
    assert row["name_en"] == "DRIVE BELT"
    assert row["name_ru"] == "ПРИВОДНОЙ РЕМЕНЬ"
    assert row["manufacturer"] == "BRP"
    assert row["country"] == "CANADA"
    assert row["usd_price"] == Decimal("19.63")
    assert row["gross_weight_kg"] is None and row["net_weight_kg"] is None
    assert row["application_area"] == ""  # утверждённое ручное поле
    # Экспорт ничего не пишет в базу: ни карточки, ни версии не появляются.
    assert not PartCustomsInfo.objects.filter(part_type=part).exists()
    assert not PartCustomsDataVersion.objects.filter(part_type=part).exists()


def test_brp_replacement_supplies_usd_but_keeps_article(part_factory):
    part = part_factory()
    BrpCatalogPart.objects.create(
        material_no="420931285", part_desc="DRIVE BELT NEW", wholesale_price_usd=None,
        replacement_no_1="420931284",
    )
    BrpCatalogPart.objects.create(
        material_no="420931284", part_desc="DRIVE BELT",
        wholesale_price_usd=Decimal("19.63"),
    )
    row = _customs_row_from_version(part, None, Decimal("1"), number="420931285")
    assert row["number"] == "420931285"  # replacement меняет источник цены, не артикул
    assert row["usd_price"] == Decimal("19.63")
    assert row["name_en"] == "DRIVE BELT NEW"  # описание - самой позиции


def test_aftermarket_fills_catalog_fields_but_country_stays_blank(part_factory):
    part = part_factory("SPI")
    _aftermarket(part)
    row = _customs_row_from_version(part, None, Decimal("3"), number="SM-01357")
    assert row["name_en"] == "SPI STATOR SKI DOO"
    assert row["name_ru"] == "SPI СТАТОР SKI-DOO"
    assert row["manufacturer"] == "SPI"
    assert row["usd_price"] == Decimal("203.26")
    # Страна SPI - утверждённое ручное поле: fallback КАНАДА только для BRP.
    assert row["country"] == ""


def test_saved_version_wins_over_catalog(part_factory):
    part = part_factory("BRP")
    BrpCatalogPart.objects.create(
        material_no="420931284", part_desc="DRIVE BELT",
        wholesale_price_usd=Decimal("19.63"),
    )
    version = PartCustomsDataVersion.objects.create(
        part_type=part, version=1, customs_name_en="SAVED NAME",
        customs_name_ru="СОХРАНЁННОЕ", manufacturer="SAVEDM",
        country_of_origin="AUSTRIA", customs_unit_price_usd=Decimal("1.00"),
        effective_from=timezone.now(),
    )
    row = _customs_row_from_version(part, version, Decimal("1"), number="420931284")
    assert row["name_en"] == "SAVED NAME"
    assert row["name_ru"] == "СОХРАНЁННОЕ"
    assert row["manufacturer"] == "SAVEDM"
    assert row["country"] == "AUSTRIA"
    assert row["usd_price"] == Decimal("1.00")


def test_xlsx_shows_catalog_fill_and_approved_blanks(part_factory):
    brp = part_factory()
    BrpCatalogPart.objects.create(
        material_no="420931284", part_desc="DRIVE BELT",
        wholesale_price_usd=Decimal("19.63"),
    )
    spi = part_factory("SPI")
    _aftermarket(spi, number="SM-09374", desc="SPI PTO CRANK WEB", usd="127.21")
    rows = [
        _customs_row_from_version(brp, None, Decimal("2"), number="420931284"),
        _customs_row_from_version(spi, None, Decimal("3"), number="SM-09374"),
    ]
    sheet = openpyxl.load_workbook(export_customs_xlsx(rows=rows)).active
    assert sheet["B10"].value == "420931284"
    assert sheet["C10"].value == "ПРИВОДНОЙ РЕМЕНЬ"
    assert sheet["D10"].value == "DRIVE BELT"
    assert sheet["E10"].value == "BRP"
    assert sheet["F10"].value == "CANADA"
    assert sheet["J10"].value == 2
    assert sheet["K10"].value == 19.63
    assert sheet["B11"].value == "SM-09374"
    assert sheet["E11"].value == "SPI"
    assert sheet["F11"].value is None  # страна SPI дозаполняется вручную
    assert sheet["K11"].value == 127.21
    # Утверждённые ручные поля пусты у обеих строк: трекинг, веса, область.
    for r in (10, 11):
        assert sheet[f"A{r}"].value is None
        assert sheet[f"G{r}"].value is None
        assert sheet[f"H{r}"].value is None
        assert sheet[f"M{r}"].value is None
