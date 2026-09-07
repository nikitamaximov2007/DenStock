"""Запомненные таможенные данные деталей для складских сценариев в тестах.

Проведённая продажа и выдача в ремонт стали таможенным источником: без веса и
области применения провести их нельзя (см. apps.actions.cart). Сценарные тесты
ниже проверяют склад, документы и деньги, а не полноту таможенной карточки,
поэтому недостающие значения подставляются здесь ровно один раз - так же, как
их подставляет сотруднику карточка детали.

Значения только ДОПОЛНЯЮТ карточку: тест, который сам задал вес или область,
своего значения не теряет.
"""
from decimal import Decimal

from apps.actions.cart import cart_rows
from apps.actions.models import PartCustomsInfo

GROSS_WEIGHT_KG = Decimal("0.350")
NET_WEIGHT_KG = Decimal("0.300")
APPLICATION_AREA = PartCustomsInfo.ApplicationArea.SNOWMOBILE


def remember_customs(*parts):
    """Дозаполнить карточки деталей до состояния «можно провести»."""
    for part in parts:
        customs, _ = PartCustomsInfo.objects.get_or_create(part_type=part)
        filled = {}
        if customs.gross_weight_kg is None:
            filled["gross_weight_kg"] = GROSS_WEIGHT_KG
        if customs.net_weight_kg is None:
            filled["net_weight_kg"] = NET_WEIGHT_KG
        if not customs.application_area:
            filled["application_area"] = APPLICATION_AREA
        if not filled:
            continue
        for field, value in filled.items():
            setattr(customs, field, value)
        customs.save(update_fields=[*filled, "updated_at"])


def remember_cart_customs(cart):
    """То же самое для всех деталей корзины прямо перед её проведением."""
    remember_customs(*[row.part for row in cart_rows(cart)])
    return cart
