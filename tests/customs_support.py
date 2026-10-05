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


def link_brp_catalog(part, number=None):
    """Связать готовую карточку с позицией BRP-каталога, как это делает импорт.

    Для таможни ручная деталь - всегда аналог; оригиналом может быть только
    импортированная. Происхождение «импорт» - запись ``BrpPartLink``, которую
    в рабочем коде создаёт только ``apps.brp.services.promote_to_warehouse``.
    Тесты, которым нужна оригинальная BRP-деталь на собственной карточке,
    ставят ту же связь здесь.
    """
    from apps.brp.models import BrpCatalogPart, BrpPartLink

    if number is None:
        primary = part.numbers.order_by("-is_primary", "pk").first()
        number = primary.value if primary else f"BRP-{part.pk}"
    brp_part, _ = BrpCatalogPart.objects.get_or_create(
        material_no=number, defaults={"part_desc": part.name[:200]},
    )
    BrpPartLink.objects.create(
        part=part, brp_part=brp_part,
        usd_rate_used=Decimal("100"), markup_percent_used=Decimal("0"),
    )
    return part


def link_aftermarket_catalog(part, manufacturer_name, number=None):
    """Связать карточку с записью каталога аналогов (PROX, BRONCO, ...).

    То же происхождение, что оставляет импорт
    ``apps.catalog_import.aftermarket_catalog.apply_file``: запись
    ``AftermarketCatalogPart`` с производителем каталога.
    """
    from apps.catalog.models import Manufacturer, normalize_number
    from apps.catalog_import.models import AftermarketCatalogPart

    if number is None:
        primary = part.numbers.order_by("-is_primary", "pk").first()
        number = primary.value if primary else f"AM-{part.pk}"
    manufacturer, _ = Manufacturer.objects.get_or_create(name=manufacturer_name)
    AftermarketCatalogPart.objects.create(
        source=AftermarketCatalogPart.SOURCE_DEALER_2023, part=part,
        manufacturer=manufacturer, manufacturer_number=number,
        normalized_manufacturer_number=normalize_number(number),
        source_description=part.name[:200],
    )
    return part
