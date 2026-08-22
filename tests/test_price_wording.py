"""Одно поле - одно название на всех экранах.

В карточке детали хранится одна цена: та, которую предлагается взять с
клиента. Экраны называли её по-разному - «Рекомендуемая цена», «Реком. цена»,
«Цена продажи: рек.», «Цена клиента», - и человеку приходилось догадываться,
одно это число или четыре разных.

Отдельно от неё живёт цена конкретной продажи: она вводится в строке продажи и
может отличаться. Эти два понятия смешивать нельзя, поэтому за каждым закреплено
своё название.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CATALOG_PRICE = "Рекомендуемая цена"

# Экраны, где показывается цена из карточки детали.
SCREENS = (
    "templates/catalog/part_detail.html",
    "templates/catalog/part_list.html",
    "templates/core/search.html",
    "templates/actions/scan.html",
)

# Названия, от которых отказались. Каждое означало то же самое число.
ABANDONED = ("Реком. цена", "Цена клиента", "Цена продажи: рек.")


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
    """У строки продажи цена своя, и путать её с карточкой нельзя."""
    forms_source = read("apps/sales/forms.py")
    assert "Цена продажи за ед. (₽)" in forms_source
    assert CATALOG_PRICE not in forms_source
