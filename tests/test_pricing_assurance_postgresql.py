"""Гарантии ценообразования после полного снимка каталога BRP.

Проверяется главное практическое свойство: после применения нового полного
снимка система не может отдать цену из прошлого каталога. Данные синтетические,
путь настоящий: файл Excel того же формата проходит через тот же импортёр, а
цены считаются теми же функциями, что и в интерфейсе.

Курс и наценка задаются детерминированно, оптовая и розничная цены намеренно
сильно расходятся, чтобы тест не мог случайно пройти на неправильном поле.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from openpyxl import Workbook

from apps.brp.importer import import_catalog
from apps.brp.models import BrpCatalogPart, BrpPricingSettings
from apps.brp.pricing import (
    VIN_SURCHARGE_USD,
    catalog_part_price_rub,
    customer_price_rub,
    effective_wholesale_usd,
    status_surcharge_usd,
)
from apps.catalog.services import get_current_price_settings
from apps.counting.services import find_brp_price_source
from apps.warehouse.models import ValuationSettings

RATE = Decimal("100")
MARKUP = Decimal("50")  # проценты: множитель 1.5
HEADERS = [
    "Material_No", "Part_Desc", "Last_Yr_Util", "Status",
    "РОЗНИЦА", "ОПТОВАЯ", "ЗАМЕНА НОМЕРА", "ЗАМЕНА НОМЕРА",
]

# Розница намеренно огромная: если где-то используется она, а не оптовая,
# результат разойдётся на порядок и тест не пройдёт случайно.
RETAIL_DECOY = 999


@pytest.fixture
def pricing(db):
    valuation = ValuationSettings.get()
    valuation.current_usd_rate = RATE
    valuation.save(update_fields=["current_usd_rate", "updated_at"])
    settings = BrpPricingSettings.get()
    settings.brp_markup_percent = MARKUP
    settings.save(update_fields=["brp_markup_percent", "updated_at"])
    return get_current_price_settings()


def write_snapshot(tmp_path, name, rows):
    """Собрать файл поставщика того же формата, что приходит от BRP."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    for row in rows:
        sheet.append([
            row["material"],
            row.get("desc", "Деталь"),
            row.get("util", ""),
            row.get("status", ""),
            row.get("retail", RETAIL_DECOY),
            row.get("wholesale", ""),
            row.get("repl1", ""),
            row.get("repl2", ""),
        ])
    path = tmp_path / name
    workbook.save(path)
    return path


def apply_snapshot(tmp_path, name, rows):
    return import_catalog(write_snapshot(tmp_path, name, rows), commit=True)


def price_of(material, pricing):
    """Цена так, как её получает интерфейс: через источник цены каталога."""
    row = BrpCatalogPart.objects.filter(material_no=material, is_current=True).first()
    if row is None:
        return None
    source = find_brp_price_source(row.material_no_norm, row)
    return catalog_part_price_rub(source, pricing.current_usd_rate, pricing.brp_markup_percent)


OLD = [
    {"material": "A", "wholesale": 10},
    {"material": "B", "wholesale": 20},
    {"material": "C", "wholesale": 30, "status": "VIN"},
    {"material": "D", "wholesale": 40},
    {"material": "E", "wholesale": 50},
]
NEW = [
    {"material": "A", "wholesale": 15},
    {"material": "B", "wholesale": 25},
    {"material": "C", "wholesale": 35, "status": "VIN"},
    # D намеренно отсутствует в новом снимке
    {"material": "E", "wholesale": 50},
    {"material": "F", "wholesale": 60},
    {"material": "G", "wholesale": 70, "status": " UCP ", "repl1": "A"},
    {"material": "H", "wholesale": 80, "status": "OBS"},
    {"material": "I", "wholesale": 90, "status": "LIQ"},
]


@pytest.fixture
def after_new_snapshot(tmp_path, pricing):
    apply_snapshot(tmp_path, "old.xlsx", OLD)
    summary = apply_snapshot(tmp_path, "new.xlsx", NEW)
    return summary


# --- Полный снимок: состав каталога ---------------------------------------------------


def test_new_snapshot_updates_raw_prices(after_new_snapshot, pricing):
    for material, expected in (("A", 15), ("B", 25), ("C", 35), ("E", 50)):
        row = BrpCatalogPart.objects.get(material_no=material)
        assert row.wholesale_price_usd == Decimal(expected), material
        assert row.is_current is True


def test_missing_row_becomes_inactive_but_is_not_deleted(after_new_snapshot):
    row = BrpCatalogPart.objects.get(material_no="D")
    assert row.is_current is False, "пропавшая строка обязана стать неактуальной"
    assert row.wholesale_price_usd == Decimal("40"), "историческая цена сохраняется в строке"


def test_new_position_appears_as_current(after_new_snapshot):
    assert BrpCatalogPart.objects.get(material_no="F").is_current is True


def test_statuses_survive_the_snapshot(after_new_snapshot):
    assert BrpCatalogPart.objects.get(material_no="H").brp_status == "OBS"
    assert BrpCatalogPart.objects.get(material_no="I").brp_status == "LIQ"
    assert BrpCatalogPart.objects.get(material_no="H").is_current is True
    assert BrpCatalogPart.objects.get(material_no="I").is_current is True


def test_ucp_is_stored_as_use(after_new_snapshot):
    """UCP это псевдоним USE, в базу попадает канонический код."""
    assert BrpCatalogPart.objects.get(material_no="G").brp_status == "USE"


# --- Самая важная гарантия: неактуальная строка не даёт текущую цену --------------------


def test_inactive_row_is_never_selected_as_a_current_price_source(
    after_new_snapshot, pricing
):
    """Строка D выпала из снимка. Её старая цена не должна стать текущей."""
    inactive = BrpCatalogPart.objects.get(material_no="D")
    assert inactive.is_current is False
    assert inactive.wholesale_price_usd == Decimal("40")

    # Через обычный путь интерфейса позиции D просто нет среди текущих.
    assert price_of("D", pricing) is None

    # И поиск источника цены по её номеру не возвращает неактуальную строку.
    source = find_brp_price_source(inactive.material_no_norm, None)
    assert source is None or source.is_current is True, (
        "источником текущей цены выбрана неактуальная строка каталога"
    )


def test_current_lookup_never_returns_an_inactive_row(after_new_snapshot):
    from apps.counting.services import find_brp_by_number

    assert find_brp_by_number("D") is None, "неактуальная строка найдена как текущая"
    assert find_brp_by_number("A") is not None


def test_persisted_recommended_price_is_not_taken_from_an_inactive_row(
    tmp_path, pricing, django_user_model
):
    """Связанная деталь с выпавшей строкой не получает цену из неё."""
    from apps.brp.services import promote_to_warehouse
    from apps.catalog.services import refresh_linked_part_prices

    apply_snapshot(tmp_path, "old.xlsx", OLD)
    row_d = BrpCatalogPart.objects.get(material_no="D")
    part = promote_to_warehouse(row_d, by=None)
    linked_before = part.recommended_price
    assert linked_before == Decimal("40") * RATE * Decimal("1.5")

    apply_snapshot(tmp_path, "new.xlsx", NEW)
    refresh_linked_part_prices(
        usd_rate=pricing.current_usd_rate,
        brp_markup=pricing.brp_markup_percent,
        polaris_markup=Decimal("0"),
    )

    part.refresh_from_db()
    row_d.refresh_from_db()
    assert row_d.is_current is False

    # Гарантия сильнее, чем «не пересчитывать»: прежнее значение снимается, чтобы
    # его нельзя было принять за текущую цену поставщика.
    assert part.recommended_price is None, (
        "у детали осталась цена, хотя её строка выпала из каталога"
    )
    stale = Decimal("40") * RATE * Decimal("1.5")
    assert part.recommended_price != stale, "цена взята из неактуальной строки"
    assert linked_before == stale, "проверка бессмысленна: до снимка цены не было"


# --- Новая цена вытесняет старую --------------------------------------------------------


@pytest.mark.parametrize(
    ("material", "old_wholesale", "new_wholesale"),
    [("A", 10, 15), ("B", 20, 25)],
)
def test_updated_price_replaces_the_previous_one(
    after_new_snapshot, pricing, material, old_wholesale, new_wholesale
):
    expected = Decimal(new_wholesale) * RATE * Decimal("1.5")
    stale = Decimal(old_wholesale) * RATE * Decimal("1.5")
    assert price_of(material, pricing) == expected
    assert price_of(material, pricing) != stale


def test_vin_position_uses_the_new_raw_price(after_new_snapshot, pricing):
    """C: было 30, стало 35. Итог обязан считаться от 35 + 25, а не от 30 + 25."""
    assert price_of("C", pricing) == (Decimal("35") + VIN_SURCHARGE_USD) * RATE * Decimal("1.5")
    assert price_of("C", pricing) != (Decimal("30") + VIN_SURCHARGE_USD) * RATE * Decimal("1.5")


def test_persisted_price_follows_the_new_snapshot(tmp_path, pricing):
    from apps.brp.services import promote_to_warehouse
    from apps.catalog.services import refresh_linked_part_prices

    apply_snapshot(tmp_path, "old.xlsx", OLD)
    part = promote_to_warehouse(BrpCatalogPart.objects.get(material_no="A"), by=None)
    assert part.recommended_price == Decimal("10") * RATE * Decimal("1.5")

    apply_snapshot(tmp_path, "new.xlsx", NEW)
    refresh_linked_part_prices(
        usd_rate=pricing.current_usd_rate,
        brp_markup=pricing.brp_markup_percent,
        polaris_markup=Decimal("0"),
    )
    part.refresh_from_db()
    assert part.recommended_price == Decimal("15") * RATE * Decimal("1.5"), (
        "сохранённая цена осталась от прошлого снимка"
    )


# --- Надбавка VIN ровно один раз --------------------------------------------------------


def test_vin_raw_wholesale_is_never_mutated(after_new_snapshot):
    assert BrpCatalogPart.objects.get(material_no="C").wholesale_price_usd == Decimal("35")


def test_vin_effective_wholesale_is_raw_plus_twenty_five(after_new_snapshot):
    row = BrpCatalogPart.objects.get(material_no="C")
    assert effective_wholesale_usd(row) == Decimal("60")


def test_repeated_calculation_does_not_stack_the_surcharge(after_new_snapshot):
    row = BrpCatalogPart.objects.get(material_no="C")
    first = effective_wholesale_usd(row)
    second = effective_wholesale_usd(row)
    third = effective_wholesale_usd(BrpCatalogPart.objects.get(material_no="C"))
    assert first == second == third == Decimal("60"), "надбавка накопилась при повторе"


def test_reapplying_the_same_snapshot_does_not_bake_the_surcharge_in(tmp_path, pricing):
    apply_snapshot(tmp_path, "s1.xlsx", NEW)
    apply_snapshot(tmp_path, "s2.xlsx", NEW)
    row = BrpCatalogPart.objects.get(material_no="C")
    assert row.wholesale_price_usd == Decimal("35"), "надбавка записана в сырую цену"
    assert effective_wholesale_usd(row) == Decimal("60")


def test_non_vin_rows_get_no_surcharge(after_new_snapshot):
    for material in ("A", "B", "E", "F", "G", "H", "I"):
        row = BrpCatalogPart.objects.get(material_no=material)
        assert status_surcharge_usd(row.brp_status) == Decimal("0"), material
        assert effective_wholesale_usd(row) == row.wholesale_price_usd, material


def test_vin_without_a_wholesale_price_creates_nothing(db):
    row = BrpCatalogPart(material_no="X", brp_status="VIN", wholesale_price_usd=None)
    assert effective_wholesale_usd(row) is None, "надбавка создала цену из ничего"
    assert catalog_part_price_rub(row, RATE, MARKUP) is None


# --- UCP ведёт себя как USE --------------------------------------------------------------


def test_ucp_row_is_priced_exactly_like_a_use_row(after_new_snapshot, pricing):
    ucp = BrpCatalogPart.objects.get(material_no="G")
    assert ucp.brp_status == "USE"
    assert status_surcharge_usd(ucp.brp_status) == Decimal("0"), "UCP получил надбавку VIN"
    assert price_of("G", pricing) == Decimal("70") * RATE * Decimal("1.5")


# --- Оптовая, а не розничная --------------------------------------------------------------


def test_price_is_built_on_wholesale_not_retail(tmp_path, pricing):
    """Розница намеренно 999: если бы использовалась она, итог был бы иным."""
    apply_snapshot(tmp_path, "w.xlsx", [
        {"material": "W", "wholesale": 10, "retail": RETAIL_DECOY},
    ])
    row = BrpCatalogPart.objects.get(material_no="W")
    assert row.retail_price_usd == Decimal(RETAIL_DECOY)
    assert row.wholesale_price_usd == Decimal("10")
    assert price_of("W", pricing) == Decimal("10") * RATE * Decimal("1.5")
    assert price_of("W", pricing) != Decimal(RETAIL_DECOY) * RATE * Decimal("1.5")


# --- Округление ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wholesale", "expected"),
    [
        ("1.0049", 100),   # 100.49 -> вниз
        ("1.005", 101),    # 100.50 -> вверх по ROUND_HALF_UP
        ("1.0051", 101),   # 100.51 -> вверх
        ("1.0149", 101),
        ("1.015", 102),
    ],
)
def test_round_half_up_at_the_boundary(db, wholesale, expected):
    assert customer_price_rub(Decimal(wholesale), Decimal("100"), Decimal("0")) == Decimal(
        expected
    )


def test_only_the_result_is_rounded(db):
    """Курс и наценка в промежуточных вычислениях не округляются."""
    exact = Decimal("7.39") * Decimal("105") * Decimal("1.4")
    assert customer_price_rub(Decimal("7.39"), Decimal("105"), Decimal("40")) == exact.quantize(
        Decimal("1")
    )


# --- Атомарность применения ----------------------------------------------------------------


def test_a_failed_snapshot_keeps_the_previous_catalog(tmp_path, pricing, monkeypatch):
    """Сбой в середине применения не должен оставить половину новых цен."""
    apply_snapshot(tmp_path, "old.xlsx", OLD)
    before = {row.material_no: row.wholesale_price_usd for row in BrpCatalogPart.objects.all()}

    from apps.brp import importer

    def boom(*args, **kwargs):
        raise RuntimeError("сбой в середине применения")

    monkeypatch.setattr(importer, "_flush", boom)
    with pytest.raises(RuntimeError):
        apply_snapshot(tmp_path, "new.xlsx", NEW)

    after = {row.material_no: row.wholesale_price_usd for row in BrpCatalogPart.objects.all()}
    assert after == before, "после сбоя каталог смешал старые и новые цены"
    assert BrpCatalogPart.objects.filter(is_current=False).count() == 0


# --- Повторное применение -------------------------------------------------------------------


def test_reapply_is_idempotent_for_prices_and_rows(tmp_path, pricing):
    apply_snapshot(tmp_path, "old.xlsx", OLD)
    apply_snapshot(tmp_path, "new.xlsx", NEW)
    first = {
        row.material_no: (row.wholesale_price_usd, row.is_current, row.brp_status)
        for row in BrpCatalogPart.objects.all()
    }
    count = BrpCatalogPart.objects.count()

    apply_snapshot(tmp_path, "new-again.xlsx", NEW)

    second = {
        row.material_no: (row.wholesale_price_usd, row.is_current, row.brp_status)
        for row in BrpCatalogPart.objects.all()
    }
    assert second == first, "повторное применение изменило каталог"
    assert BrpCatalogPart.objects.count() == count, "повторное применение создало дубликаты"


# --- Замены ------------------------------------------------------------------------------


def test_replacement_price_comes_from_a_current_row(tmp_path, pricing):
    """У позиции нет цены, замена указывает на текущую позицию с ценой."""
    apply_snapshot(tmp_path, "repl.xlsx", [
        {"material": "P1", "wholesale": 0, "repl1": "P2"},
        {"material": "P2", "wholesale": 12},
    ])
    p1 = BrpCatalogPart.objects.get(material_no="P1")
    source = find_brp_price_source(p1.material_no_norm, p1)
    assert source is not None and source.material_no == "P2"
    assert source.is_current is True
    assert catalog_part_price_rub(source, RATE, MARKUP) == Decimal("12") * RATE * Decimal("1.5")


def test_replacement_never_prices_from_an_inactive_predecessor(tmp_path, pricing):
    """Замена указывает на позицию, выпавшую из нового снимка."""
    apply_snapshot(tmp_path, "r1.xlsx", [
        {"material": "Q1", "wholesale": 0, "repl1": "Q2"},
        {"material": "Q2", "wholesale": 77},
    ])
    apply_snapshot(tmp_path, "r2.xlsx", [
        {"material": "Q1", "wholesale": 0, "repl1": "Q2"},
    ])
    q2 = BrpCatalogPart.objects.get(material_no="Q2")
    assert q2.is_current is False

    q1 = BrpCatalogPart.objects.get(material_no="Q1")
    source = find_brp_price_source(q1.material_no_norm, q1)
    assert source is None or source.is_current is True, (
        "цена взята из выпавшей из каталога позиции-предшественника"
    )


# --- Единообразие путей интерфейса ----------------------------------------------------------


def _promoted_part(tmp_path, rows, material):
    from apps.brp.services import promote_to_warehouse

    apply_snapshot(tmp_path, "ui-old.xlsx", rows)
    return promote_to_warehouse(BrpCatalogPart.objects.get(material_no=material), by=None)


def test_finance_report_does_not_price_from_an_inactive_row(tmp_path, pricing):
    """Отчёт стоимости склада обязан подчиняться тому же правилу актуальности.

    Он считает текущую клиентскую цену по связанной строке каталога. Если строка
    выпала из снимка, цену из неё брать нельзя: иначе один экран показывает цену,
    которой поставщик больше не публикует, а другой не показывает ничего.
    """
    from apps.reports.warehouse_finance import _sale_price_rub

    part = _promoted_part(tmp_path, OLD, "D")
    apply_snapshot(tmp_path, "ui-new.xlsx", NEW)
    row_d = BrpCatalogPart.objects.get(material_no="D")
    assert row_d.is_current is False

    part.refresh_from_db()
    part = type(part).objects.select_related("brp_link__brp_part").get(pk=part.pk)
    brp_settings = BrpPricingSettings.get()
    from apps.polaris.models import PolarisPricingSettings

    price = _sale_price_rub(
        part, RATE, brp_settings, PolarisPricingSettings.get(), None, None
    )
    stale = Decimal("40") * RATE * Decimal("1.5")
    assert price != stale, (
        "отчёт стоимости склада посчитал цену по выпавшей из каталога строке"
    )


def test_every_ui_path_agrees_on_a_current_row(tmp_path, pricing):
    """Один и тот же номер обязан давать одну цену на всех путях."""
    from apps.core.receiving_queue import _brp_candidate
    from apps.counting.services import _effective_brp_price

    apply_snapshot(tmp_path, "ui.xlsx", [{"material": "U1", "wholesale": 10}])
    row = BrpCatalogPart.objects.get(material_no="U1")
    expected = Decimal("10") * RATE * Decimal("1.5")

    assert price_of("U1", pricing) == expected
    assert _effective_brp_price(row.material_no_norm, row) == expected
    assert _brp_candidate(row, pricing).unit_price == expected


def test_every_ui_path_agrees_on_a_vin_row(tmp_path, pricing):
    from apps.core.receiving_queue import _brp_candidate
    from apps.counting.services import _effective_brp_price

    apply_snapshot(tmp_path, "uv.xlsx", [{"material": "V1", "wholesale": 35, "status": "VIN"}])
    row = BrpCatalogPart.objects.get(material_no="V1")
    expected = (Decimal("35") + VIN_SURCHARGE_USD) * RATE * Decimal("1.5")

    assert price_of("V1", pricing) == expected
    assert _effective_brp_price(row.material_no_norm, row) == expected
    assert _brp_candidate(row, pricing).unit_price == expected


# --- Остатки на полке живут дольше каталога ---------------------------------------------


def _with_stock(part, quantity="3"):
    """Физический остаток на полке. Деталь уже стоит на складе, независимо от каталога."""
    from apps.inventory.models import StockLot
    from apps.procurement.models import Batch, BatchLine
    from apps.suppliers.models import Supplier
    from apps.warehouse.models import StorageLocation

    supplier, _ = Supplier.objects.get_or_create(name="Поставщик проверки цен")
    location, _ = StorageLocation.objects.get_or_create(
        code="S99-L01-D01-C01",
        defaults={"name": "Ячейка проверки цен", "storage_allowed": True, "is_active": True},
    )
    batch = Batch.objects.create(supplier=supplier)
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal("100"),
    )
    return StockLot.objects.create(
        part_type=part,
        batch=batch,
        batch_line=line,
        location=location,
        quantity=Decimal(quantity),
        initial_quantity=Decimal(quantity),
        status=StockLot.Status.AVAILABLE,
    )


def test_discontinued_row_with_stock_stays_findable_and_priced(tmp_path, pricing):
    """Снятая с производства позиция продаётся с полки и обязана иметь цену.

    Статус OBS означает «снято с производства», но строка остаётся в актуальном
    снимке. Поставщик её публикует, значит цена есть, и продавец должен видеть
    и деталь, и цену.
    """
    from apps.core.search import search_parts

    rows = [{"material": "OBS-1001", "wholesale": 80, "status": "OBS"}]
    part = _promoted_part(tmp_path, rows, "OBS-1001")
    _with_stock(part)

    expected = Decimal("80") * RATE * Decimal("1.5")
    part.refresh_from_db()
    assert part.recommended_price == expected, "снятая с производства позиция осталась без цены"

    found = search_parts("OBS-1001")
    shown = next((c for c in found if c.part.id == part.id), None)
    assert shown is not None, "деталь с остатком не находится поиском"
    assert shown.client_price == expected, "поиск показал не ту цену"


def test_part_with_a_withdrawn_row_stays_findable_without_a_catalog_price(tmp_path, pricing):
    """Остаток никуда не делся, а каталожной цены больше нет.

    Строка выпала из снимка. Деталь обязана остаться в поиске, иначе товар с
    полки пропадёт для продавца. Но цену показывать нельзя: поставщик её больше
    не публикует. Пустая цена честнее устаревшей.
    """
    from apps.catalog.services import refresh_linked_part_prices
    from apps.core.search import search_parts

    before = [{"material": "GONE-2002", "wholesale": 40}]
    after = [{"material": "STAY-3003", "wholesale": 55}]
    part = _promoted_part(tmp_path, before, "GONE-2002")
    _with_stock(part)
    stale = Decimal("40") * RATE * Decimal("1.5")
    part.refresh_from_db()
    assert part.recommended_price == stale, "проверка бессмысленна: до снимка цены не было"

    apply_snapshot(tmp_path, "gone.xlsx", after)
    refresh_linked_part_prices(
        usd_rate=pricing.current_usd_rate,
        brp_markup=pricing.brp_markup_percent,
        polaris_markup=Decimal("0"),
    )
    assert BrpCatalogPart.objects.get(material_no="GONE-2002").is_current is False

    found = search_parts("GONE-2002")
    shown = next((c for c in found if c.part.id == part.id), None)
    assert shown is not None, "деталь с реальным остатком пропала из поиска после снимка"
    assert shown.client_price != stale, "поиск показал цену по выпавшей строке"
    assert shown.client_price is None


def test_a_large_current_catalog_stays_answerable(tmp_path, pricing):
    """Цена одной позиции не должна зависеть от размера каталога.

    Если бы выбор источника цены перебирал каталог, на боевом объёме поиск цены
    встал бы. Тест фиксирует, что запросов на одну цену — единицы, а не тысячи.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    rows = [{"material": f"P{i}", "wholesale": 10 + (i % 50)} for i in range(3000)]
    apply_snapshot(tmp_path, "big.xlsx", rows)
    assert BrpCatalogPart.objects.filter(is_current=True).count() == 3000

    target = BrpCatalogPart.objects.get(material_no="P1500")
    with CaptureQueriesContext(connection) as captured:
        price = catalog_part_price_rub(
            find_brp_price_source(target.material_no_norm, target),
            pricing.current_usd_rate,
            pricing.brp_markup_percent,
        )
    assert price == (Decimal("10") + Decimal(1500 % 50)) * RATE * Decimal("1.5")
    assert len(captured) <= 3, (
        f"выбор источника цены сделал {len(captured)} запросов: каталог перебирается целиком"
    )
