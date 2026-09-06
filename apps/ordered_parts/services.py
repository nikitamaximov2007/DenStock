"""Бизнес-правила раздела «Запчасти на заказ». View сюда только оркестрирует.

Артикул разбирается КАНОНИЧЕСКИМ поиском склада (`resolve_part_lookup`), а не
вторым собственным движком: иначе одна и та же строка находила бы разные детали
в разных экранах. Здесь только сужение правил под заказ:

* заказ оформляется на ОРИГИНАЛЬНУЮ деталь, поэтому позиция из каталога
  аналогов отклоняется явным сообщением, а не превращается молча в оригинал;
* неоднозначный артикул не выбирается за оператора: он получает список;
* ненайденный артикул не создаёт фиктивную карточку каталога.

Наличие детали на складе не требуется вовсе: заказывают как раз то, чего нет.
"""
from decimal import Decimal

from django.db import transaction

from apps.catalog_import.origin import AFTERMARKET_CATALOG, aftermarket_part_ids
from apps.core.part_lookup import (
    clean_lookup_value,
    lookup_part_by_id,
    part_not_found_message,
    resolve_part_lookup,
)

from .models import OrderedPart

ANALOG_REJECTED_MESSAGE = (
    "Эта деталь заведена каталогом аналогов. «Запчасти на заказ» оформляются "
    "только на оригинальные детали."
)
AMBIGUOUS_MESSAGE = "Найдено несколько деталей с таким артикулом. Выберите нужную."


class OrderedPartError(ValueError):
    """Заказ оформить нельзя: артикул, клиент или предоплата не проходят правило."""


def is_aftermarket_part(part) -> bool:
    """Заведена ли деталь каталогом аналогов.

    Единственный канонический признак «аналога» в текущем DenisStock: запись в
    `AftermarketCatalogPart`. Направленная связь `PartAnalog` сюда НЕ входит -
    она означает «эта деталь подходит вместо той», и оригинал от неё аналогом
    не становится.

    ЕДИНСТВЕННАЯ точка этого решения во всём разделе. Когда таможенный экспорт
    разделят на обычный и аналоговый, перецелить нужно только её.
    """
    return bool(aftermarket_part_ids([part.pk]))


def resolve_ordered_article(raw):
    """Найти оригинальную деталь по артикулу. Возвращает (candidate, result).

    ``candidate`` не None только когда деталь определена однозначно И является
    оригиналом. Во всех остальных случаях поднимается OrderedPartError с
    текстом, который можно показать оператору как есть.
    """
    query = clean_lookup_value(raw)
    if not query:
        raise OrderedPartError("Укажите артикул запчасти.")
    result = resolve_part_lookup(query, include_price=True)
    if result.status == "not_found":
        raise OrderedPartError(
            f"{part_not_found_message(query)} "
            "Деталь с таким артикулом в загруженных каталогах не найдена."
        )
    if not result.found:
        # «ambiguous» и «multiple» одинаково означают, что выбор за оператором.
        raise OrderedPartError(AMBIGUOUS_MESSAGE)
    candidate = result.candidate
    if is_aftermarket_part(candidate.part):
        raise OrderedPartError(ANALOG_REJECTED_MESSAGE)
    return candidate, result


def resolve_ordered_part_by_id(part_id):
    """Деталь, выбранная оператором из списка неоднозначного артикула."""
    from apps.catalog.models import PartType

    part = PartType.objects.filter(pk=part_id).first()
    if part is None:
        raise OrderedPartError("Деталь не найдена.")
    if is_aftermarket_part(part):
        raise OrderedPartError(ANALOG_REJECTED_MESSAGE)
    return lookup_part_by_id(part, include_price=True)


def parse_prepayment(raw) -> Decimal:
    """Предоплата - Decimal, ноль допустим, отрицательная нет.

    Ноль это нормальное состояние: клиент мог договориться, но ещё не перевести.
    Требовать положительную сумму означало бы запретить такой заказ.
    """
    if raw is None or str(raw).strip() == "":
        return Decimal("0")
    try:
        value = Decimal(str(raw).strip().replace(",", ".").replace(" ", ""))
    except (ArithmeticError, ValueError) as exc:
        raise OrderedPartError("Предоплата должна быть числом.") from exc
    if value.is_nan() or value.is_infinite():
        raise OrderedPartError("Предоплата должна быть числом.")
    if value < 0:
        raise OrderedPartError("Предоплата не может быть отрицательной.")
    return value.quantize(Decimal("0.01"))


def _catalog_source(candidate) -> str:
    """Каким каталогом деталь доказана - для снимка в заказе."""
    part = candidate.part
    if getattr(part, "brp_link_id", None) or _has_relation(part, "brp_link"):
        return "brp"
    if _has_relation(part, "polaris_link"):
        return "polaris"
    if candidate.catalog_origin == AFTERMARKET_CATALOG:
        return "aftermarket"
    return "catalog"


def _has_relation(part, attribute) -> bool:
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return getattr(part, attribute, None) is not None
    except ObjectDoesNotExist:
        return False


@transaction.atomic
def create_ordered_part(*, candidate, customer, prepayment, by=None) -> OrderedPart:
    """Оформить заказ. Складских последствий нет и быть не должно.

    Ни лота, ни движения, ни продажи, ни приёмки здесь не создаётся: заказанной
    детали физически ещё нет.
    """
    if customer is None:
        raise OrderedPartError("Выберите клиента.")
    if candidate is None:
        raise OrderedPartError("Выберите деталь по артикулу.")
    if is_aftermarket_part(candidate.part):
        raise OrderedPartError(ANALOG_REJECTED_MESSAGE)
    return OrderedPart.objects.create(
        customer=customer,
        part_type=candidate.part,
        article=candidate.exact_number or "",
        part_name=candidate.part.name,
        manufacturer_name=candidate.manufacturer or "",
        catalog_source=_catalog_source(candidate),
        prepayment_rub=parse_prepayment(prepayment),
        created_by=by,
    )


@transaction.atomic
def update_ordered_part(order: OrderedPart, *, customer, prepayment, by=None) -> OrderedPart:
    """Исправить ошибку оператора: клиент и предоплата.

    Снимок детали не правится: заказ оформлен на конкретную деталь, и подмена
    её задним числом переписала бы историю. Ошиблись деталью - нужен отдельный
    продуктовый ответ, а не тихая замена.
    """
    if customer is None:
        raise OrderedPartError("Выберите клиента.")
    order.customer = customer
    order.prepayment_rub = parse_prepayment(prepayment)
    order.updated_by = by
    order.save(update_fields=["customer", "prepayment_rub", "updated_by", "updated_at"])
    return order


def ordered_parts_list():
    """Все заказы, новые сверху. Read-only."""
    return OrderedPart.objects.select_related("customer", "part_type", "created_by")
