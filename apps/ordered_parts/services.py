"""Бизнес-правила раздела «Запчасти на заказ». View сюда только оркестрирует.

Артикул сначала разбирается КАНОНИЧЕСКИМ поиском складских карточек
(`resolve_part_lookup`), а затем - теми же нормализованными номерами в
импортированных справочных каталогах. Второго формата артикулов здесь нет:
иначе одна и та же строка находила бы разные детали в разных экранах. Здесь
только сужение правил под заказ:

* заказ оформляется на ОРИГИНАЛЬНУЮ деталь, поэтому позиция из каталога
  аналогов отклоняется явным сообщением, а не превращается молча в оригинал;
* неоднозначный артикул не выбирается за оператора: он получает список;
* ненайденный артикул не создаёт фиктивную карточку каталога.

Наличие детали на складе не требуется вовсе: заказывают как раз то, чего нет.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from apps.catalog.models import normalize_number
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


@dataclass
class OrderedCatalogCandidate:
    """Imported catalog identity not promoted to a warehouse card yet.

    ``OrderedPart`` keeps a stable PartType snapshot, so promotion is deferred
    until the operator actually submits the order. A GET/search therefore stays
    read-only; promotion creates no stock and is the existing catalog-to-card
    service used later by warehouse workflows.
    """

    part: object | None
    catalog_part: object
    exact_number: str
    manufacturer: str
    display_name: str
    client_price: Decimal | None
    catalog_origin: str

    @property
    def catalog_origin_label(self) -> str:
        return self.catalog_origin


def _customs_analog_verdict(part):
    """Что о детали думает классификатор таможенной выгрузки, если он уже есть.

    Выгрузку сейчас делят на обычную и аналоговую в соседней ветке, и там
    появляется свой канонический ответ на вопрос «это аналог?». Пока его в
    сборке нет, функция возвращает None; как только он появится, раздел заказов
    начнёт спрашивать именно его и второго контракта аналогов не возникнет.
    """
    try:
        from apps.actions.customs_history import _is_analog_part
    except ImportError:
        return None
    from apps.catalog.models import PartAnalog

    links = PartAnalog.objects.filter(Q(analog_id=part.pk) | Q(original_id=part.pk))
    analog_ids = {pk for pk in links.values_list("analog_id", flat=True)}
    original_ids = {pk for pk in links.values_list("original_id", flat=True)}
    try:
        return bool(_is_analog_part(part, analog_ids, original_ids))
    except TypeError:
        # Сигнатура у соседа изменилась: молча пропускать деталь нельзя,
        # но и врать про её вид тоже. Пусть решает базовое правило.
        return None


def is_analog_part(part) -> bool:
    """Аналог ли деталь. ЕДИНСТВЕННАЯ точка этого решения во всём разделе.

    Правило намеренно строже каждого из источников по отдельности: деталь
    считается аналогом, если так говорит каталог аналогов ИЛИ канонический
    классификатор таможенной выгрузки. Ошибиться можно в две стороны, и цена у
    них разная. Лишний отказ оператор видит сразу и обходит. Лишнее разрешение
    тихо уводит заказанную деталь в обычную выгрузку, тогда как её же продажа
    уходит в аналоговую, и расхождение всплывёт уже на таможне.
    """
    if aftermarket_part_ids([part.pk]):
        return True
    return bool(_customs_analog_verdict(part))


# Прежнее имя: раздел спрашивает «аналог ли», а каталог аналогов лишь один из
# ответов на этот вопрос.
is_aftermarket_part = is_analog_part


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
        catalog_candidates = _imported_catalog_candidates(normalize_number(query))
        if len(catalog_candidates) == 1:
            return catalog_candidates[0], result
        if len(catalog_candidates) > 1:
            raise OrderedPartError(AMBIGUOUS_MESSAGE)
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


def _catalog_price(catalog_part, source: str) -> Decimal | None:
    """Calculate a catalog price without creating missing settings on lookup."""
    from apps.warehouse.models import ValuationSettings

    valuation = ValuationSettings.objects.order_by("pk").first()
    if valuation is None:
        return None
    if source == "brp":
        from apps.brp.models import BrpPricingSettings
        from apps.brp.pricing import catalog_part_price_rub

        settings = BrpPricingSettings.objects.order_by("pk").first()
        return (
            catalog_part_price_rub(catalog_part, valuation.current_usd_rate,
                                   settings.brp_markup_percent)
            if settings else None
        )
    if source == "polaris":
        from apps.polaris.models import PolarisPricingSettings
        from apps.polaris.pricing import customer_price_rub

        settings = PolarisPricingSettings.objects.order_by("pk").first()
        return (
            customer_price_rub(catalog_part.wholesale_price_usd,
                               valuation.current_usd_rate,
                               settings.polaris_markup_percent)
            if settings else None
        )
    return None


def _imported_catalog_candidates(norm: str) -> list[OrderedCatalogCandidate]:
    """Find eligible imported rows without requiring a warehouse PartType."""
    from apps.brp.models import BrpCatalogPart, BrpPartLink
    from apps.polaris.models import PolarisCatalogPart, PolarisPartLink

    candidates = []
    brp = (
        BrpCatalogPart.objects.filter(material_no_norm=norm, is_current=True)
        .order_by("pk")
        .first()
    )
    if brp is not None:
        linked = BrpPartLink.objects.filter(brp_part=brp).select_related("part").first()
        candidates.append(
            OrderedCatalogCandidate(
                part=linked.part if linked else None,
                catalog_part=brp,
                exact_number=brp.material_no,
                manufacturer="BRP",
                display_name=brp.part_desc or f"BRP {brp.material_no}",
                client_price=_catalog_price(brp, "brp"),
                catalog_origin="BRP",
            )
        )
    polaris = PolarisCatalogPart.objects.filter(part_number_norm=norm).order_by("pk").first()
    if polaris is not None:
        linked = (
            PolarisPartLink.objects.filter(polaris_part=polaris)
            .select_related("part")
            .first()
        )
        candidates.append(
            OrderedCatalogCandidate(
                part=linked.part if linked else None,
                catalog_part=polaris,
                exact_number=polaris.part_number,
                manufacturer="POLARIS",
                display_name=polaris.part_name or f"POLARIS {polaris.part_number}",
                client_price=_catalog_price(polaris, "polaris"),
                catalog_origin="POLARIS",
            )
        )
    return candidates


def _ensure_ordered_part_card(candidate, *, by=None):
    """Resolve a catalog candidate to the stable PartType snapshot for an order."""
    if candidate.part is not None:
        return candidate.part
    if candidate.catalog_origin == "BRP":
        from apps.brp.services import promote_to_warehouse

        return promote_to_warehouse(candidate.catalog_part, by=by)
    if candidate.catalog_origin == "POLARIS":
        from apps.polaris.services import promote_to_warehouse

        return promote_to_warehouse(candidate.catalog_part, by=by)
    raise OrderedPartError("Для этой позиции нет канонической карточки каталога.")


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
    if isinstance(candidate, OrderedCatalogCandidate):
        return candidate.catalog_origin.lower()
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
    part = _ensure_ordered_part_card(candidate, by=by)
    if is_aftermarket_part(part):
        raise OrderedPartError(ANALOG_REJECTED_MESSAGE)
    part_name = getattr(candidate, "display_name", None) or candidate.part.name
    return OrderedPart.objects.create(
        customer=customer,
        part_type=part,
        article=candidate.exact_number or "",
        part_name=part_name,
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
