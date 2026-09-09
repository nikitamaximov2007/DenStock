"""Запомненные таможенные данные деталей для складских сценариев в тестах.

Проведённая продажа и выдача в ремонт стали таможенным источником: без веса и
области применения провести их нельзя (см. apps.actions.cart). Сценарные тесты
ниже проверяют склад, документы и деньги, а не полноту таможенной карточки,
поэтому недостающие значения подставляются здесь ровно один раз - так же, как
их подставляет сотруднику карточка детали.

Значения только ДОПОЛНЯЮТ карточку: тест, который сам задал вес или область,
своего значения не теряет.
"""
from contextlib import contextmanager
from decimal import Decimal

from apps.actions.cart import cart_rows
from apps.actions.models import PartCustomsDataVersion, PartCustomsInfo

GROSS_WEIGHT_KG = Decimal("0.250")
NET_WEIGHT_KG = Decimal("0.200")
APPLICATION_AREA = PartCustomsInfo.ApplicationArea.WATERCRAFT


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


@contextmanager
def legacy_customs_completion(*parts):
    """Провести документ так, как его проводили ДО обязательных таможенных полей.

    Новый контракт требует вес и применимость при КАЖДОМ проведении. Часть
    тестов проверяет не его, а поведение выгрузки на ИСТОРИЧЕСКИХ данных: такие
    продажи и ремонты в базе есть, переписать их задним числом нельзя, и
    выгрузка обязана их пережить.

    Поэтому метаданные подставляются только на время проведения и возвращаются
    ровно в прежнее состояние - включая версии, которые создал post_save-сигнал
    карточки. Записи очищаются через queryset, чтобы сигнал не сработал снова.
    """
    ids = [part.pk for part in parts]
    before = {
        row.part_type_id: (row.gross_weight_kg, row.net_weight_kg, row.application_area)
        for row in PartCustomsInfo.objects.filter(part_type_id__in=ids)
    }
    versions = set(
        PartCustomsDataVersion.objects.filter(part_type_id__in=ids).values_list("pk", flat=True)
    )
    # Не «дозаполнить», а именно задать валидную тройку: историческая карточка
    # может нести и легаси-область («МОТО ЗАПЧАСТИ», «КАТЕР / ЛОДКА»), и пару
    # весов, которую новый контракт уже не принимает. Прежние значения
    # возвращаются целиком, поэтому проверяемый тестом факт не меняется.
    for part in parts:
        customs, _ = PartCustomsInfo.objects.get_or_create(part_type=part)
        customs.gross_weight_kg = GROSS_WEIGHT_KG
        customs.net_weight_kg = NET_WEIGHT_KG
        customs.application_area = APPLICATION_AREA
        customs.save(
            update_fields=[
                "gross_weight_kg", "net_weight_kg", "application_area", "updated_at",
            ]
        )
    try:
        yield
    finally:
        PartCustomsDataVersion.objects.filter(part_type_id__in=ids).exclude(
            pk__in=versions
        ).delete()
        for part_id in ids:
            rows = PartCustomsInfo.objects.filter(part_type_id=part_id)
            if part_id in before:
                gross, net, area = before[part_id]
                rows.update(gross_weight_kg=gross, net_weight_kg=net, application_area=area)
            else:
                rows.delete()
