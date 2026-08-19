"""Граница между удержанием прежней цены и запретом на неактуальные строки.

Это два разных правила, и их легко перепутать.

Импортёр имеет право перенести прежнюю положительную оптовую цену в НОВУЮ
актуальную строку, когда поставщик прислал по позиции ноль или пусто. Тогда
цена живёт в актуальной строке, и брать её можно.

Слой цен НЕ имеет права заглянуть в строку, выпавшую из снимка, и взять цену
оттуда. Внешне результат похож, та же сумма, но разница принципиальная: в
первом случае цену подтвердил импортёр текущего снимка, во втором её взяли из
позиции, которой поставщик больше не публикует.

Тесты проверяют именно эту границу.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from openpyxl import Workbook

from apps.brp.importer import import_catalog
from apps.brp.models import BrpCatalogPart, BrpPricingSettings
from apps.brp.pricing import catalog_part_price_rub
from apps.catalog.services import get_current_price_settings
from apps.counting.services import find_brp_price_source
from apps.warehouse.models import ValuationSettings

RATE = Decimal("100")
MARKUP = Decimal("50")  # проценты: множитель 1.5
HEADERS = [
    "Material_No", "Part_Desc", "Last_Yr_Util", "Status",
    "РОЗНИЦА", "ОПТОВАЯ", "ЗАМЕНА НОМЕРА", "ЗАМЕНА НОМЕРА",
]
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


def apply_snapshot(tmp_path, name, rows):
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
    return import_catalog(path, commit=True)


def price_of(material, pricing):
    """Цена так, как её получает интерфейс."""
    row = BrpCatalogPart.objects.filter(material_no=material, is_current=True).first()
    if row is None:
        return None
    source = find_brp_price_source(row.material_no_norm, row)
    return catalog_part_price_rub(source, pricing.current_usd_rate, pricing.brp_markup_percent)


# --- A: новая положительная цена побеждает ------------------------------------------------


def test_new_positive_wholesale_wins(tmp_path, pricing):
    apply_snapshot(tmp_path, "a1.xlsx", [{"material": "AA-1", "wholesale": 10}])
    apply_snapshot(tmp_path, "a2.xlsx", [{"material": "AA-1", "wholesale": 15}])

    row = BrpCatalogPart.objects.get(material_no="AA-1")
    assert row.is_current is True
    assert row.wholesale_price_usd == Decimal("15")
    assert price_of("AA-1", pricing) == Decimal("15") * RATE * Decimal("1.5")


# --- B: ноль в новом файле, прежняя положительная переносится в АКТУАЛЬНУЮ строку ---------


def test_new_zero_keeps_previous_positive_in_the_current_row(tmp_path, pricing):
    """Цена остаётся у актуальной строки, а не берётся из выбывшей.

    Это разрешённый случай: снимок содержит позицию, значит поставщик её
    публикует, просто без цены. Прежняя цена переносится импортёром в ту же
    актуальную строку, и слой цен читает именно её.
    """
    apply_snapshot(tmp_path, "b1.xlsx", [{"material": "BB-1", "wholesale": 10}])
    apply_snapshot(tmp_path, "b2.xlsx", [{"material": "BB-1", "wholesale": 0}])

    row = BrpCatalogPart.objects.get(material_no="BB-1")
    assert row.is_current is True, "позиция есть в новом снимке, значит актуальна"
    assert row.wholesale_price_usd == Decimal("10"), (
        "прежняя положительная цена не перенесена в актуальную строку"
    )
    assert price_of("BB-1", pricing) == Decimal("10") * RATE * Decimal("1.5")


def test_the_retained_price_comes_from_a_current_row_not_an_inactive_one(tmp_path, pricing):
    """Ключевое различие: источник цены обязан быть актуальной строкой.

    Сумма совпала бы и при неправильной реализации, поэтому проверяется не
    сумма, а сам источник.
    """
    apply_snapshot(tmp_path, "b3.xlsx", [{"material": "BB-2", "wholesale": 10}])
    apply_snapshot(tmp_path, "b4.xlsx", [{"material": "BB-2", "wholesale": 0}])

    row = BrpCatalogPart.objects.get(material_no="BB-2")
    source = find_brp_price_source(row.material_no_norm, row)
    assert source is not None
    assert source.is_current is True, "цена пришла из неактуальной строки"
    assert source.pk == row.pk, "цена пришла не из той строки, что показана в каталоге"
    assert BrpCatalogPart.objects.filter(material_no="BB-2").count() == 1, (
        "импортёр создал вторую строку вместо обновления существующей"
    )


# --- C: пригодной цены нет нигде ----------------------------------------------------------


def test_no_usable_price_anywhere_stays_empty(tmp_path, pricing):
    """Ложный ноль не должен становиться настоящей ценой."""
    apply_snapshot(tmp_path, "c1.xlsx", [{"material": "CC-1", "wholesale": 0}])

    row = BrpCatalogPart.objects.get(material_no="CC-1")
    assert row.is_current is True
    assert row.wholesale_price_usd is None, "ноль поставщика сохранён как настоящая цена"
    assert price_of("CC-1", pricing) is None


def test_empty_wholesale_does_not_become_zero_price(tmp_path, pricing):
    apply_snapshot(tmp_path, "c2.xlsx", [{"material": "CC-2", "wholesale": ""}])

    row = BrpCatalogPart.objects.get(material_no="CC-2")
    assert row.wholesale_price_usd is None
    assert price_of("CC-2", pricing) is None


# --- D: выбывшая строка никогда не участвует в текущем ценообразовании --------------------


def test_a_withdrawn_row_is_never_a_current_price_source(tmp_path, pricing):
    """Позиции нет в новом снимке: её прежняя цена ценой быть перестаёт.

    Отличие от случая B: там позиция ОСТАЛАСЬ в снимке без цены, здесь она из
    снимка исчезла. В первом случае цена переносится, во втором нет.
    """
    apply_snapshot(tmp_path, "d1.xlsx", [{"material": "DD-1", "wholesale": 10}])
    apply_snapshot(tmp_path, "d2.xlsx", [{"material": "OTHER-1", "wholesale": 20}])

    withdrawn = BrpCatalogPart.objects.get(material_no="DD-1")
    assert withdrawn.is_current is False
    assert withdrawn.wholesale_price_usd == Decimal("10"), (
        "историческая цена должна остаться в базе как история"
    )
    assert find_brp_price_source(withdrawn.material_no_norm, withdrawn) is None, (
        "выбывшая строка выбрана источником текущей цены"
    )
    assert price_of("DD-1", pricing) is None


def test_the_two_rules_are_not_confused(tmp_path, pricing):
    """Обе позиции стоили 10. Одна осталась в снимке без цены, другая выпала.

    Первая обязана сохранить цену, вторая обязана её потерять. Один и тот же
    снимок, разный итог.
    """
    apply_snapshot(tmp_path, "e1.xlsx", [
        {"material": "STAY-1", "wholesale": 10},
        {"material": "DROP-1", "wholesale": 10},
    ])
    apply_snapshot(tmp_path, "e2.xlsx", [{"material": "STAY-1", "wholesale": 0}])

    stayed = BrpCatalogPart.objects.get(material_no="STAY-1")
    dropped = BrpCatalogPart.objects.get(material_no="DROP-1")

    assert stayed.is_current is True and dropped.is_current is False
    assert price_of("STAY-1", pricing) == Decimal("10") * RATE * Decimal("1.5")
    assert price_of("DROP-1", pricing) is None


# --- G: VIN без оптовой цены не создаёт цену из надбавки ----------------------------------


def test_vin_without_a_wholesale_price_does_not_become_a_surcharge_price(tmp_path, pricing):
    """Надбавка VIN не является ценой сама по себе."""
    apply_snapshot(tmp_path, "g1.xlsx", [{"material": "GG-1", "status": "VIN", "wholesale": ""}])

    row = BrpCatalogPart.objects.get(material_no="GG-1")
    assert row.brp_status == "VIN"
    assert row.wholesale_price_usd is None
    assert price_of("GG-1", pricing) is None, "позиция без оптовой цены получила цену из надбавки"


# --- I, J: OBS и LIQ остаются допустимыми актуальными строками ----------------------------


@pytest.mark.parametrize("status", ["OBS", "LIQ", "USE", "UCP", ""])
def test_any_status_in_the_snapshot_stays_a_valid_current_row(tmp_path, pricing, status):
    """Статус не решает актуальность: решает присутствие в снимке."""
    material = f"ST-{status or 'BLANK'}"
    apply_snapshot(tmp_path, f"s-{material}.xlsx", [
        {"material": material, "wholesale": 20, "status": status},
    ])

    row = BrpCatalogPart.objects.get(material_no=material)
    assert row.is_current is True
    assert price_of(material, pricing) == Decimal("20") * RATE * Decimal("1.5")
