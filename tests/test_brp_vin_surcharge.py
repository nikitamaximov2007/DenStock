"""Надбавка BRP за винтажный склад (статус VIN).

Правило поставщика: к позиции со статусом VIN добавляется доставка 25 USD с
винтажного склада.

Что здесь гарантируется:

* сырая оптовая цена каталога НЕ меняется: в базе остаётся цена производителя,
  иначе следующий импорт того же прайса выглядел бы как изменение цены;
* надбавку применяет единственный централизованный слой цен, а не импортёр и
  не экраны;
* надбавку получает ТОЛЬКО VIN: OBS, USE, LIQ, пустой и неизвестный статус
  считаются без неё;
* смена статуса без изменения цены всё равно меняет рассчитанную цену, и
  обратный переход возвращает её назад;
* история не переписывается: проведённые продажи, себестоимость лотов и
  складские движения остаются прежними.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group

from apps.brp.models import BrpCatalogPart, BrpPartLink, BrpPricingSettings
from apps.brp.pricing import (
    VIN_SURCHARGE_USD,
    catalog_part_price_rub,
    customer_price_rub,
    effective_wholesale_usd,
    status_surcharge_usd,
)
from apps.brp.services import promote_to_warehouse
from apps.inventory.models import StockMovement
from apps.warehouse.models import ValuationSettings

PASSWORD = "parol-12345"
RATE = Decimal("105")
MARKUP = Decimal("40")


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


@pytest.fixture
def pricing(db):
    valuation = ValuationSettings.get()
    valuation.current_usd_rate = RATE
    valuation.save()
    settings = BrpPricingSettings.get()
    settings.brp_markup_percent = MARKUP
    settings.save()
    return valuation, settings


def _part(status="", wholesale="100", material="420831955"):
    return BrpCatalogPart.objects.create(
        material_no=material,
        part_desc="ROLLER",
        brp_status=status,
        wholesale_price_usd=Decimal(wholesale),
    )


# --- Эффективная оптовая цена ---------------------------------------------------------------


def test_ordinary_effective_wholesale_is_raw_price(db):
    assert effective_wholesale_usd(_part()) == Decimal("100")


def test_vin_effective_wholesale_adds_surcharge(db):
    assert effective_wholesale_usd(_part(status="VIN")) == Decimal("125")
    assert VIN_SURCHARGE_USD == Decimal("25")


def test_vin_raw_wholesale_is_never_modified(db):
    part = _part(status="VIN")
    catalog_part_price_rub(part, RATE, MARKUP)
    part.refresh_from_db()
    assert part.wholesale_price_usd == Decimal("100")  # цена производителя цела


@pytest.mark.parametrize("status", ["OBS", "USE", "LIQ", "", "ZZZ", "UCP"])
def test_only_vin_gets_surcharge(db, status):
    assert status_surcharge_usd(status) == Decimal("0")
    assert effective_wholesale_usd(_part(status=status)) == Decimal("100")


def test_status_comparison_is_normalized(db):
    assert status_surcharge_usd(" vin ") == VIN_SURCHARGE_USD
    assert effective_wholesale_usd(_part(status="vin")) == Decimal("125")


def test_missing_wholesale_stays_missing(db):
    part = BrpCatalogPart.objects.create(material_no="777", brp_status="VIN")
    assert effective_wholesale_usd(part) is None
    assert catalog_part_price_rub(part, RATE, MARKUP) is None


# --- Формула и округление -------------------------------------------------------------------


def test_vin_uses_the_same_formula_and_rounding(db, pricing):
    ordinary = _part(material="1")
    vintage = _part(material="2", status="VIN")
    # Та же цепочка: (опт + надбавка) * курс * (1 + наценка/100), ROUND_HALF_UP.
    assert catalog_part_price_rub(ordinary, RATE, MARKUP) == customer_price_rub(
        Decimal("100"), RATE, MARKUP
    )
    assert catalog_part_price_rub(vintage, RATE, MARKUP) == customer_price_rub(
        Decimal("125"), RATE, MARKUP
    )
    assert catalog_part_price_rub(ordinary, RATE, MARKUP) == Decimal("14700")
    assert catalog_part_price_rub(vintage, RATE, MARKUP) == Decimal("18375")


def test_existing_settings_are_still_used(db, pricing):
    valuation, settings = pricing
    settings.brp_markup_percent = Decimal("0")
    settings.save()
    valuation.current_usd_rate = Decimal("100")
    valuation.save()
    assert catalog_part_price_rub(_part(status="VIN"), Decimal("100"), Decimal("0")) == Decimal(
        "12500"
    )


# --- Пересчёт при смене статуса ---------------------------------------------------------------


def _refresh_prices():
    from apps.catalog.services import get_current_price_settings, refresh_linked_part_prices

    settings = get_current_price_settings()
    return refresh_linked_part_prices(
        usd_rate=settings.current_usd_rate,
        brp_markup=settings.brp_markup_percent,
        polaris_markup=settings.polaris_markup_percent,
        catalogs=frozenset({"brp"}),
    )


def _linked_price(part):
    link = BrpPartLink.objects.get(brp_part=part)
    link.part.refresh_from_db()
    return link.part.recommended_price


def test_status_change_to_vin_recalculates_linked_part(db, admin, pricing):
    part = _part()
    promote_to_warehouse(part, by=admin)
    before = _linked_price(part)

    # Новый прайс: цена та же, статус стал VIN.
    BrpCatalogPart.objects.filter(pk=part.pk).update(brp_status="VIN")
    _refresh_prices()

    after = _linked_price(part)
    assert after > before
    assert after == customer_price_rub(Decimal("125"), RATE, MARKUP)
    part.refresh_from_db()
    assert part.wholesale_price_usd == Decimal("100")  # сырая цена не тронута


def test_status_change_back_from_vin_removes_surcharge(db, admin, pricing):
    part = _part(status="VIN")
    promote_to_warehouse(part, by=admin)
    BrpCatalogPart.objects.filter(pk=part.pk).update(brp_status="")
    _refresh_prices()
    assert _linked_price(part) == customer_price_rub(Decimal("100"), RATE, MARKUP)


def test_wholesale_change_with_vin_recalculates(db, admin, pricing):
    part = _part(status="VIN")
    promote_to_warehouse(part, by=admin)
    BrpCatalogPart.objects.filter(pk=part.pk).update(wholesale_price_usd=Decimal("120"))
    _refresh_prices()
    assert _linked_price(part) == customer_price_rub(Decimal("145"), RATE, MARKUP)


@pytest.mark.parametrize("status", ["OBS", "USE", "LIQ"])
def test_other_statuses_do_not_change_linked_price(db, admin, pricing, status):
    part = _part()
    promote_to_warehouse(part, by=admin)
    before = _linked_price(part)
    BrpCatalogPart.objects.filter(pk=part.pk).update(brp_status=status)
    _refresh_prices()
    assert _linked_price(part) == before


# --- Историческая и складская безопасность -----------------------------------------------------


def test_recalculation_does_not_rewrite_history_or_stock(db, admin, pricing):
    from apps.inventory.models import StockBalance, StockLot
    from apps.sales.models import Sale

    part = _part(status="")
    promote_to_warehouse(part, by=admin)

    movements_before = StockMovement.objects.count()
    lots_before = list(StockLot.objects.values_list("pk", "landed_unit_cost_rub"))
    balances_before = StockBalance.objects.count()
    sales_before = list(Sale.objects.values_list("pk", "revenue_total"))

    BrpCatalogPart.objects.filter(pk=part.pk).update(brp_status="VIN")
    _refresh_prices()

    assert StockMovement.objects.count() == movements_before
    assert list(StockLot.objects.values_list("pk", "landed_unit_cost_rub")) == lots_before
    assert StockBalance.objects.count() == balances_before
    assert list(Sale.objects.values_list("pk", "revenue_total")) == sales_before


def test_repeated_refresh_is_idempotent(db, admin, pricing):
    part = _part(status="VIN")
    promote_to_warehouse(part, by=admin)
    _refresh_prices()
    first = _linked_price(part)
    _refresh_prices()
    assert _linked_price(part) == first


# --- Надбавка применяется во ВСЕХ путях расчёта -----------------------------------------------


def test_all_brp_price_paths_apply_the_surcharge(db, pricing):
    """Одна и та же деталь не должна стоить по-разному на разных экранах.

    Регрессия release candidate: часть путей расчёта передавала в формулу сырую
    оптовую цену и обходила надбавку VIN.
    """
    from apps.brp.pricing import effective_wholesale_usd
    from apps.counting.services import find_brp_price_source
    from apps.reports.warehouse_finance import _brp_base_usd

    vintage = _part(status="VIN", material="900000001")
    expected = customer_price_rub(Decimal("125"), RATE, MARKUP)

    # Прямой расчёт по позиции каталога.
    assert catalog_part_price_rub(vintage, RATE, MARKUP) == expected

    # Источник цены (он же позиция при отсутствии замены).
    source = find_brp_price_source(vintage.material_no_norm, vintage)
    assert catalog_part_price_rub(source, RATE, MARKUP) == expected

    # Финансовый отчёт склада отдаёт УЖЕ расчётную оптовую цену.
    assert _brp_base_usd(vintage) == Decimal("125")
    assert effective_wholesale_usd(vintage) == Decimal("125")


def test_ordinary_part_price_paths_are_unchanged(db, pricing):
    from apps.reports.warehouse_finance import _brp_base_usd

    ordinary = _part(material="900000002")
    assert _brp_base_usd(ordinary) == Decimal("100")
    assert catalog_part_price_rub(ordinary, RATE, MARKUP) == customer_price_rub(
        Decimal("100"), RATE, MARKUP
    )
