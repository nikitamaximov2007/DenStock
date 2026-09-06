"""Утверждённый BRP fallback страны не меняет другие бренды и историю."""

from decimal import Decimal

import openpyxl
import pytest
from django.utils import timezone

from apps.actions.models import PartCustomsDataVersion, PartCustomsInfo
from apps.actions.services import (
    _customs_row_from_version,
    export_customs_xlsx,
    part_export_data,
    resolve_customs_country,
    system_customs_facts,
)
from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.catalog.models import Category, Manufacturer, PartType, Unit
from apps.catalog_import.models import AftermarketCatalogPart
from apps.polaris.models import PolarisCatalogPart, PolarisPartLink

pytestmark = pytest.mark.django_db


@pytest.fixture
def part_factory():
    category = Category.objects.create(name="Country test")
    unit = Unit.objects.get(name="Штука")

    def create(brand=None):
        manufacturer = Manufacturer.objects.get_or_create(name=brand)[0] if brand else None
        return PartType.objects.create(
            name="Country test", category=category, unit=unit, manufacturer=manufacturer
        )

    return create


@pytest.mark.parametrize("brand,expected", [("BRP", "CANADA"), (" brp ", "CANADA"),
                                           ("POLARIS", ""), ("SPI", ""), (None, "")])
def test_only_identified_brp_gets_country(part_factory, brand, expected):
    part = part_factory(brand)
    assert resolve_customs_country(part) == expected
    assert part_export_data(part)["country"] == expected
    assert system_customs_facts(part)["country_of_origin"] == expected
    assert not PartCustomsInfo.objects.filter(part_type=part).exists()


@pytest.mark.parametrize("brand", ["BRP", "POLARIS", "SPI", None])
def test_explicit_country_wins_without_rewriting_versions(part_factory, brand):
    part = part_factory(brand)
    info = PartCustomsInfo.objects.create(part_type=part, country_of_origin="AUSTRIA")
    version = PartCustomsDataVersion.objects.get(part_type=part)
    before = list(PartCustomsDataVersion.objects.filter(part_type=part).values())
    assert _customs_row_from_version(part, version, Decimal("1"))["country"] == "AUSTRIA"
    assert system_customs_facts(part)["country_of_origin"] == "AUSTRIA"
    assert part_export_data(part)["country"] == "AUSTRIA"
    info.refresh_from_db()
    assert info.country_of_origin == "AUSTRIA"
    assert list(PartCustomsDataVersion.objects.filter(part_type=part).values()) == before


def test_catalog_link_proves_brp_without_manufacturer(part_factory):
    part = part_factory()
    BrpPartLink.objects.create(
        part=part, brp_part=BrpCatalogPart.objects.create(material_no="BRP-TEST"),
        usd_rate_used=Decimal("90"), markup_percent_used=Decimal("40"),
    )
    assert resolve_customs_country(part) == "CANADA"


@pytest.mark.parametrize("catalog", ["polaris", "aftermarket"])
def test_other_catalog_link_prevents_brp_default(part_factory, catalog):
    part = part_factory("BRP")  # legacy/inconsistent manufacturer label
    if catalog == "polaris":
        PolarisPartLink.objects.create(
            part=part, polaris_part=PolarisCatalogPart.objects.create(part_number="P-TEST"),
            usd_rate_used=Decimal("90"), markup_percent_used=Decimal("40"),
        )
    else:
        AftermarketCatalogPart.objects.create(
            part=part, source="dealer_2023", manufacturer=part.manufacturer,
            manufacturer_number="SM-TEST", source_description="TEST",
        )
    assert resolve_customs_country(part) == ""


def test_xlsx_brp_fallback_preserves_empty_weights_and_explicit_snapshot(part_factory):
    brp = part_factory("BRP")
    spi = part_factory("SPI")
    version = PartCustomsDataVersion.objects.create(
        part_type=brp, version=1, country_of_origin="AUSTRIA", effective_from=timezone.now()
    )
    rows = [
        _customs_row_from_version(brp, None, Decimal("2")),
        _customs_row_from_version(spi, None, Decimal("3")),
        _customs_row_from_version(brp, version, Decimal("4")),
    ]
    sheet = openpyxl.load_workbook(export_customs_xlsx(rows=rows)).active
    assert [sheet[f"F{r}"].value for r in range(10, 13)] == ["CANADA", None, "AUSTRIA"]
    assert [sheet[f"J{r}"].value for r in range(10, 13)] == [2, 3, 4]
    assert all(sheet[f"{col}{r}"].value is None for col in "GH" for r in range(10, 13))
    version.refresh_from_db()
    assert version.country_of_origin == "AUSTRIA"
    assert not PartCustomsInfo.objects.exists()
