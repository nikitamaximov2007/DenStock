"""Аналог - это отдельная складская деталь, а не второй номер той же.

На складе Дениса заметная доля позиций - «закос под оригинал». На коробке
такой детали часто написан ТОТ ЖЕ артикул, что у оригинальной: номер означает,
подо что деталь сделана, а не кто её сделал.

Отсюда главное правило, которое здесь закреплено: одинаковый артикул не делает
две детали одной. У аналога свои остатки, партии, цена, штрихкоды и история.

Не путать с номером вида «Аналог» у самой детали: тот означает, что одну и ту
же деталь могут спросить под другим номером, и второй карточки за ним нет.
"""
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.catalog.models import Category, PartAnalog, PartBarcode, PartNumber, PartType, Unit
from apps.catalog.services import (
    AnalogLinkError,
    ManualPartError,
    analog_rows,
    create_analog_part,
    create_manual_part,
    link_analog,
    unlink_analog,
)
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement

SAME = "420123456"


@pytest.fixture
def part(db):
    def _make(name, article="", price=None, barcode="", manufacturer=""):
        return create_manual_part(
            name=name, article=article, price=price,
            barcode=barcode, manufacturer_name=manufacturer,
        )

    return _make


@pytest.fixture
def pair(part):
    original = part("Поршень BRP", SAME, Decimal("10000"), manufacturer="BRP")
    analog = part("Поршень XYZ", SAME, Decimal("4500"), manufacturer="XYZ")
    return original, analog


def _stock_snapshot():
    return (
        StockMovement.objects.count(),
        StockLot.objects.count(),
        PartItem.objects.count(),
        StockBalance.objects.count(),
    )


# --- Одинаковый артикул - обычное дело -------------------------------------------------


def test_two_parts_may_carry_the_very_same_article(pair):
    """Ровно тот случай, ради которого всё это делается."""
    original, analog = pair
    assert original.pk != analog.pk
    assert original.numbers.get().value == analog.numbers.get().value == SAME
    assert PartNumber.objects.filter(normalized_value=SAME).count() == 2


def test_nothing_in_the_model_forbids_a_repeated_article(pair):
    """Уникальность по артикулу сломала бы реальный склад, и её нет."""
    assert PartNumber._meta.get_field("value").unique is False
    assert not PartNumber._meta.constraints


def test_the_link_says_which_one_is_the_analog(pair):
    original, analog = pair
    link, created = link_analog(original=original, analog=analog)
    assert created is True
    assert link.original_id == original.pk
    assert link.analog_id == analog.pk


# --- Что связь запрещает ----------------------------------------------------------------


def test_a_part_cannot_be_an_analog_of_itself(part):
    only = part("Одинокая", "111")
    with pytest.raises(AnalogLinkError):
        link_analog(original=only, analog=only)
    assert not PartAnalog.objects.exists()


def test_the_database_refuses_a_self_link_too(part):
    """Связь заводится не только из формы, поэтому проверка стоит и в базе."""
    only = part("Одинокая", "111")
    with pytest.raises(IntegrityError), transaction.atomic():
        PartAnalog.objects.create(original=only, analog=only)


def test_the_same_link_twice_does_not_double(pair):
    original, analog = pair
    first, created_first = link_analog(original=original, analog=analog)
    second, created_second = link_analog(original=original, analog=analog)

    assert created_first is True
    assert created_second is False
    assert first.pk == second.pk
    assert PartAnalog.objects.count() == 1


def test_the_database_refuses_a_duplicate_pair(pair):
    original, analog = pair
    link_analog(original=original, analog=analog)
    with pytest.raises(IntegrityError), transaction.atomic():
        PartAnalog.objects.create(original=original, analog=analog)


def test_the_reverse_direction_is_refused_with_a_reason(pair):
    """Тот же факт с другой стороны. Две записи выглядели бы как две связи."""
    original, analog = pair
    link_analog(original=original, analog=analog)

    with pytest.raises(AnalogLinkError) as failure:
        link_analog(original=analog, analog=original)
    assert "Поршень XYZ" in str(failure.value) or "Поршень BRP" in str(failure.value)
    assert PartAnalog.objects.count() == 1


# --- Сколько и кого можно связывать -----------------------------------------------------


def test_one_part_may_have_many_analogs(part):
    original = part("Поршень BRP", SAME, Decimal("10000"))
    for index in range(5):
        analog = part(f"Аналог {index}", SAME if index % 2 else f"ALT-{index}")
        link_analog(original=original, analog=analog)

    assert PartAnalog.objects.filter(original=original).count() == 5
    assert len(analog_rows(original)) == 5


def test_one_analog_may_fit_several_originals(part):
    """Одна и та же неоригинальная деталь часто подходит к нескольким."""
    first = part("Поршень BRP 650", "ORIGINAL-001")
    second = part("Поршень BRP 850", "ORIGINAL-002")
    analog = part("Поршень XYZ универсальный", "ALT-002", Decimal("5000"))

    link_analog(original=first, analog=analog)
    link_analog(original=second, analog=analog)

    assert PartAnalog.objects.filter(analog=analog).count() == 2
    back = analog_rows(analog, direction="originals")
    assert {row["part"].name for row in back} == {"Поршень BRP 650", "Поршень BRP 850"}


def test_being_an_analog_is_not_passed_along_the_chain(part):
    """A аналог B, B аналог C - это НЕ значит, что A аналог C."""
    first = part("Первая", "1")
    second = part("Вторая", "2")
    third = part("Третья", "3")
    link_analog(original=first, analog=second)
    link_analog(original=second, analog=third)

    rows = analog_rows(first)
    assert [row["part"].name for row in rows] == ["Вторая"]


# --- Снятие связи -----------------------------------------------------------------------


def test_removing_the_link_keeps_both_parts(pair):
    """Связь - это мнение о деталях, а не сами детали."""
    original, analog = pair
    link, _ = link_analog(original=original, analog=analog)

    unlink_analog(link)

    assert not PartAnalog.objects.exists()
    assert PartType.objects.filter(pk=original.pk).exists()
    assert PartType.objects.filter(pk=analog.pk).exists()


def test_after_removing_the_link_it_can_be_made_again(pair):
    original, analog = pair
    link, _ = link_analog(original=original, analog=analog)
    unlink_analog(link)
    _, created = link_analog(original=original, analog=analog)
    assert created is True


# --- Остатки у аналога свои --------------------------------------------------------------


def test_making_a_link_creates_no_stock(pair):
    original, analog = pair
    before = _stock_snapshot()
    link_analog(original=original, analog=analog)
    assert _stock_snapshot() == before


def test_creating_an_analog_creates_no_stock(part):
    original = part("Поршень BRP", SAME)
    before = _stock_snapshot()
    create_analog_part(original=original, name="Поршень XYZ", article=SAME)
    assert _stock_snapshot() == before


def test_an_analog_starts_with_nothing_on_the_shelf(pair):
    original, analog = pair
    link_analog(original=original, analog=analog)
    rows = analog_rows(original)
    assert rows[0]["available"] == Decimal("0")


# --- Создание аналога одним действием ------------------------------------------------------


def test_creating_an_analog_links_it_at_once(part):
    original = part("Поршень BRP", SAME, Decimal("10000"))
    analog = create_analog_part(
        original=original, name="Поршень XYZ", article=SAME,
        price=Decimal("4500"), manufacturer_name="XYZ",
    )

    assert PartAnalog.objects.filter(original=original, analog=analog).exists()
    assert analog.recommended_price == Decimal("4500")
    assert analog.manufacturer.name == "XYZ"
    assert analog.numbers.get().value == SAME


def test_a_failed_link_leaves_no_half_made_part(part, monkeypatch):
    """Иначе деталь появилась бы, а человек считал бы, что ничего не вышло.

    Через саму форму связь после создания упасть не может: деталь только что
    заведена, и столкнуться ей не с чем. Проверяется именно граница транзакции,
    поэтому отказ вносится подменой: без общей транзакции карточка осталась бы
    висеть без связи.
    """
    from apps.catalog import services

    original = part("Поршень BRP", SAME)
    before = PartType.objects.count()

    def refuse(**kwargs):
        raise AnalogLinkError("отказ для проверки отката")

    monkeypatch.setattr(services, "link_analog", refuse)
    with pytest.raises(AnalogLinkError):
        services.create_analog_part(original=original, name="Поршень XYZ", article=SAME)

    assert PartType.objects.count() == before
    assert not PartType.objects.filter(name="Поршень XYZ").exists()


def test_an_empty_name_is_refused_by_the_same_rule_as_a_plain_part(part):
    original = part("Поршень BRP", SAME)
    with pytest.raises(ManualPartError):
        create_analog_part(original=original, name="  ")


# --- Штрихкод при создании -----------------------------------------------------------------


def test_a_barcode_can_be_given_right_away(part):
    """Коробка у оператора в руках именно в момент заведения."""
    made = part("Поршень XYZ", SAME, barcode="4600001234567")
    assert made.barcodes.get().value == "4600001234567"


def test_a_barcode_is_optional(part):
    made = part("Поршень XYZ", SAME)
    assert not made.barcodes.exists()


def test_a_taken_barcode_names_the_part_that_holds_it(part):
    """Штрихкод в модели уникален, и человеку нужно знать, чей он."""
    part("Поршень BRP", SAME, barcode="4600001234567")

    with pytest.raises(ManualPartError) as failure:
        part("Поршень XYZ", SAME, barcode="4600001234567")
    assert "Поршень BRP" in str(failure.value)
    assert PartBarcode.objects.count() == 1


def test_a_refused_barcode_leaves_no_part_behind(part):
    part("Поршень BRP", SAME, barcode="4600001234567")
    before = PartType.objects.count()
    with pytest.raises(ManualPartError):
        part("Поршень XYZ", SAME, barcode="4600001234567")
    assert PartType.objects.count() == before


def test_the_original_and_the_analog_may_have_different_barcodes(part):
    """Артикул один, штрихкоды разные - так сканер их и различает."""
    original = part("Поршень BRP", SAME, barcode="4600001111111")
    analog = part("Поршень XYZ", SAME, barcode="4600002222222")
    link_analog(original=original, analog=analog)

    from apps.core.part_lookup import resolve_part_lookup

    found = resolve_part_lookup("4600002222222")
    assert [c.part.pk for c in found.candidates] == [analog.pk]


# --- Производитель и цена необязательны ------------------------------------------------------


def test_a_manufacturer_is_optional(part):
    made = part("Поршень без завода", SAME)
    assert made.manufacturer is None


def test_a_price_is_optional_and_absent_is_not_zero(part):
    made = part("Поршень без цены", SAME)
    assert made.recommended_price is None


def test_the_row_carries_what_the_card_has_to_show(part):
    original = part("Поршень BRP", SAME, Decimal("10000"))
    part_analog = create_analog_part(
        original=original, name="Поршень XYZ", article=SAME,
        price=Decimal("4500"), manufacturer_name="XYZ",
    )
    row = analog_rows(original)[0]

    assert row["part"].pk == part_analog.pk
    assert row["exact_number"] == SAME
    assert row["manufacturer"] == "XYZ"
    assert row["price"] == Decimal("4500")
    assert row["available"] == Decimal("0")


# --- Справочник единиц и категорий не размножается ---------------------------------------------


def test_analogs_do_not_multiply_the_service_category(part):
    original = part("Поршень BRP", SAME)
    for index in range(4):
        create_analog_part(original=original, name=f"Аналог {index}", article=SAME)
    assert Category.objects.count() == 1
    assert Unit.objects.filter(name="Штука").count() == 1
