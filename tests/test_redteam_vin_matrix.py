"""Red-team: полная матрица надбавки BRP за винтажный склад.

Проверяется контракт, а не реализация: расчётная оптовая цена равна сырой цене
плюс 25 USD только для статуса VIN, во всех остальных случаях равна сырой цене,
и надбавка НИКОГДА не создаёт цену из ничего.

Отдельно проверяются переходы между статусами: важно не только то, что VIN
добавляет надбавку, но и то, что любой уход из VIN её снимает.
"""
from decimal import Decimal

import pytest

from apps.brp.models import BrpCatalogPart, BrpPricingSettings
from apps.brp.pricing import (
    catalog_part_price_rub,
    customer_price_rub,
    effective_wholesale_usd,
    status_surcharge_usd,
)
from apps.warehouse.models import ValuationSettings

RATE = Decimal("105")
MARKUP = Decimal("40")


@pytest.fixture
def pricing(db):
    valuation = ValuationSettings.get()
    valuation.current_usd_rate = RATE
    valuation.save()
    settings = BrpPricingSettings.get()
    settings.brp_markup_percent = MARKUP
    settings.save()
    return valuation, settings


def _part(status="", wholesale="100", material="900000000"):
    return BrpCatalogPart.objects.create(
        material_no=material, brp_status=status,
        wholesale_price_usd=None if wholesale is None else Decimal(wholesale),
    )


# --- Матрица расчётной оптовой цены ---------------------------------------------------------


@pytest.mark.parametrize(
    "status,raw,expected",
    [
        ("", "100", "100"),
        ("VIN", "100", "125"),
        ("VIN", "120", "145"),
        ("VIN", "0.01", "25.01"),
        ("OBS", "100", "100"),
        ("USE", "100", "100"),
        ("LIQ", "100", "100"),
        ("UCP", "100", "100"),
        ("ZZZ", "100", "100"),
    ],
)
def test_effective_wholesale_matrix(db, status, raw, expected):
    part = _part(status=status, wholesale=raw)
    assert effective_wholesale_usd(part) == Decimal(expected)


def test_surcharge_never_creates_price_from_nothing(db):
    """Позиция без оптовой цены остаётся без цены, а не стоит 25 USD."""
    part = _part(status="VIN", wholesale=None)
    assert effective_wholesale_usd(part) is None
    assert catalog_part_price_rub(part, RATE, MARKUP) is None


def test_zero_wholesale_with_vin(db):
    """Нулевая цена это известное значение, а не отсутствие цены."""
    part = _part(status="VIN", wholesale="0")
    assert effective_wholesale_usd(part) == Decimal("25")


@pytest.mark.parametrize("raw_status", ["VIN", "vin", " VIN ", "Vin", "\tvin\n"])
def test_status_normalization(raw_status):
    assert status_surcharge_usd(raw_status) == Decimal("25")


@pytest.mark.parametrize("raw_status", ["", "  ", None, "V", "VINTAGE", "VIN2", "XVIN"])
def test_near_miss_statuses_get_nothing(raw_status):
    assert status_surcharge_usd(raw_status) == Decimal("0")


# --- Переходы между статусами -----------------------------------------------------------------


@pytest.mark.parametrize("target", ["", "OBS", "USE", "LIQ", "ZZZ"])
def test_leaving_vin_removes_surcharge(db, target):
    part = _part(status="VIN")
    assert effective_wholesale_usd(part) == Decimal("125")
    BrpCatalogPart.objects.filter(pk=part.pk).update(brp_status=target)
    part.refresh_from_db()
    assert effective_wholesale_usd(part) == Decimal("100")


@pytest.mark.parametrize("source", ["", "OBS", "USE", "LIQ"])
def test_entering_vin_adds_surcharge(db, source):
    part = _part(status=source)
    assert effective_wholesale_usd(part) == Decimal("100")
    BrpCatalogPart.objects.filter(pk=part.pk).update(brp_status="VIN")
    part.refresh_from_db()
    assert effective_wholesale_usd(part) == Decimal("125")


# --- Формула и округление не изменились --------------------------------------------------------


def test_formula_chain_is_unchanged(db, pricing):
    """VIN проходит по той же цепочке и с тем же округлением."""
    assert catalog_part_price_rub(_part(material="1"), RATE, MARKUP) == customer_price_rub(
        Decimal("100"), RATE, MARKUP
    )
    assert catalog_part_price_rub(
        _part(material="2", status="VIN"), RATE, MARKUP
    ) == customer_price_rub(Decimal("125"), RATE, MARKUP)


def test_rounding_is_half_up_on_the_total_only(db, pricing):
    # 100.004 * 105 * 1.4 = 14700.588 -> 14701 при ROUND_HALF_UP по итогу.
    part = _part(material="3", wholesale="100.004")
    assert catalog_part_price_rub(part, RATE, MARKUP) == Decimal("14701")


def test_settings_change_keeps_surcharge(db, pricing):
    """Смена курса и наценки не отменяет надбавку."""
    vintage = _part(material="4", status="VIN")
    assert catalog_part_price_rub(vintage, Decimal("90"), Decimal("10")) == customer_price_rub(
        Decimal("125"), Decimal("90"), Decimal("10")
    )


def test_raw_price_is_never_written_back(db, pricing):
    part = _part(material="5", status="VIN")
    for _ in range(3):
        catalog_part_price_rub(part, RATE, MARKUP)
    part.refresh_from_db()
    assert part.wholesale_price_usd == Decimal("100")
