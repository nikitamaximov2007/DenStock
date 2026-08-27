"""Одно поле - одно название на всех экранах.

В карточке детали хранится одна цена: та, которую предлагается взять с
клиента. Экраны называли её по-разному - «Рекомендуемая цена», «Реком. цена»,
«Цена продажи: рек.», «Цена клиента», - и человеку приходилось догадываться,
одно это число или четыре разных.

Теперь у неё одно короткое имя: «Цена». Оператору нужно ровно одно число,
чтобы назвать цену клиенту, и рядом с ним не должно стоять ни второй цены, ни
себестоимости под видом цены.

Отдельно от неё живёт цена конкретной продажи: она вводится в строке продажи и
может отличаться. Эти два понятия смешивать нельзя, поэтому за каждым закреплено
своё название.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CATALOG_PRICE = "Цена"

# Экраны, где показывается цена из карточки детали. Складские попали сюда
# вместе с единым словарём: раньше у них не было цены вовсе, а самой заметной
# величиной оказывалась себестоимость лота.
SCREENS = (
    "templates/catalog/part_detail.html",
    "templates/catalog/part_list.html",
    "templates/core/search.html",
    "templates/actions/scan.html",
    "templates/inventory/lot_list.html",
    "templates/inventory/lot_detail.html",
    "templates/inventory/item_list.html",
    "templates/inventory/item_detail.html",
)

# Названия, от которых отказались. Каждое означало то же самое число.
ABANDONED = (
    "Реком. цена", "Цена клиента", "Цена продажи: рек.", "Рекомендуемая цена",
)

# Вторая цена рядом с первой читается как противоречие: минимальная цена
# осталась в модели и в правилах, но обычному оператору не показывается.
SECOND_PRICE = "Минимальная цена"


def read(relative: str) -> str:
    return open(ROOT / relative, encoding="utf-8").read()


@pytest.mark.parametrize("screen", SCREENS)
def test_every_screen_uses_the_same_name_for_the_catalog_price(screen):
    assert CATALOG_PRICE in read(screen), (
        f"{screen}: цена из карточки названа иначе, чем на остальных экранах"
    )


@pytest.mark.parametrize("screen", SCREENS)
@pytest.mark.parametrize("abandoned", ABANDONED)
def test_the_old_names_did_not_come_back(screen, abandoned):
    assert abandoned not in read(screen), f"{screen}: вернулось название «{abandoned}»"


@pytest.mark.parametrize("screen", SCREENS)
def test_no_second_price_stands_next_to_the_first(screen):
    assert SECOND_PRICE not in read(screen), (
        f"{screen}: рядом с ценой снова стоит вторая цена"
    )


def test_the_form_for_a_new_part_uses_the_same_name():
    from apps.catalog.forms import ManualPartForm

    assert CATALOG_PRICE in ManualPartForm().fields["price"].label


def test_the_form_says_plainly_that_this_is_not_the_purchase_cost():
    """Оператор заводит карточку до приёмки и должен понимать, что вводит."""
    from apps.catalog.forms import ManualPartForm

    hint = ManualPartForm().fields["price"].help_text
    assert "клиент" in hint.lower()
    assert "себестоимость" in hint.lower()


def test_the_price_of_a_single_sale_keeps_its_own_name():
    """У строки продажи цена своя, и путать её с карточкой нельзя.

    Сравнение по ПОЛНОЙ метке: короткое «Цена» входит в «Цена продажи за ед.»
    как часть слова, и проверка на вхождение здесь ничего бы не значила.
    """
    from apps.catalog.forms import ManualPartForm
    from apps.sales.forms import AddSaleItemForm, AddSaleLotForm

    catalog_label = ManualPartForm().fields["price"].label
    for form in (AddSaleItemForm(), AddSaleLotForm()):
        sale_label = form.fields["unit_price"].label
        assert sale_label == "Цена продажи за ед. (₽)"
        assert sale_label != catalog_label
