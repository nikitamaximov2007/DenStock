"""Аудит клиентской цены: сходится ли она с оптовой ценой каталога.

Своей формулы здесь нет и быть не должно. Ожидаемая цена считается ровно тем
кодом, которым её считает конвейер цен: `apps.brp.pricing.customer_price_rub`
(оптовая USD × курс × (1 + наценка/100), квантованная до целого рубля
ROUND_HALF_UP), надбавка винтажного склада, цепочка замен BRP и
`apps.procurement.models.money`. Аудит проверяет ДАННЫЕ, а не повторяет
арифметику по памяти: вторая формула рядом с первой - это способ получить два
разных ответа на один вопрос.

Ничего не пишет. Ни цену, ни снимок связи, ни склад.

Категории намеренно различают «цена не совпала» и «сверить нечем»:

* EXACT_MATCH - цена ровно та, что даёт конвейер;
* ROUNDING_ONLY_MATCH - расходится меньше рубля, то есть другим округлением;
* PRICE_MISMATCH - расходится на рубль и больше: это уже другая цена;
* WHOLESALE_SOURCE_MISSING - источника цены нет (позиция снята с каталога,
  оптовой цены нет во всей цепочке замен, прайс Polaris не содержит номера).
  Конвейер в этом случае СОХРАНЯЕТ прежнюю цену, поэтому такая цена
  исторична и проверить её нечем;
* SOURCE_INVALID - источник есть, но его оптовая цена не положительное число;
* CUSTOMER_PRICE_MISSING - оптовая цена есть, а клиентской цены нет;
* FORMULA_NOT_APPLICABLE - карточка не связана ни с одним каталогом: оптовой
  цены у неё нет в принципе, формула к ней не относится;
* MANUAL_OVERRIDE - цена помечена ручной в снимке связи. Это НЕ постоянный
  замок: следующий удачный прайс её заменяет (см. `catalog/services.py`),
  поэтому расхождение здесь ожидаемо и считается отдельно.
"""

from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal

from apps.brp.models import BrpPartLink
from apps.brp.pricing import customer_price_rub, effective_wholesale_usd, status_surcharge_usd
from apps.counting.services import find_brp_price_source
from apps.inventory.movement import live_stock_rows
from apps.inventory.presentation import manufacturer_display, part_exact_number, with_part_identity
from apps.polaris.models import PolarisPartLink
from apps.polaris.services import find_polaris_price_source
from apps.procurement.models import money

from .models import PartType

ZERO = Decimal("0")
ONE_RUB = Decimal("1")

EXACT_MATCH = "EXACT_MATCH"
ROUNDING_ONLY_MATCH = "ROUNDING_ONLY_MATCH"
PRICE_MISMATCH = "PRICE_MISMATCH"
WHOLESALE_SOURCE_MISSING = "WHOLESALE_SOURCE_MISSING"
SOURCE_INVALID = "SOURCE_INVALID"
CUSTOMER_PRICE_MISSING = "CUSTOMER_PRICE_MISSING"
FORMULA_NOT_APPLICABLE = "FORMULA_NOT_APPLICABLE"
MANUAL_OVERRIDE = "MANUAL_OVERRIDE"

CATEGORIES = (
    EXACT_MATCH,
    ROUNDING_ONLY_MATCH,
    PRICE_MISMATCH,
    WHOLESALE_SOURCE_MISSING,
    SOURCE_INVALID,
    CUSTOMER_PRICE_MISSING,
    FORMULA_NOT_APPLICABLE,
    MANUAL_OVERRIDE,
)

BRP = "brp"
POLARIS = "polaris"
AFTERMARKET = "aftermarket"
NO_SOURCE = "none"


@dataclass(frozen=True, slots=True)
class PriceAuditRow:
    """Одна деталь и её сверка. Поля отчёта, не бизнес-объект."""

    part_id: int
    category: str
    source: str
    source_reference: str
    wholesale_usd: Decimal | None
    surcharge_usd: Decimal
    expected_price: Decimal | None
    actual_price: Decimal | None
    manual: bool
    is_public: bool
    available: Decimal
    reason: str = ""
    article: str = ""
    name: str = ""
    manufacturer: str = ""

    @property
    def delta(self) -> Decimal | None:
        if self.expected_price is None or self.actual_price is None:
            return None
        return self.actual_price - self.expected_price

    @property
    def delta_percent(self) -> Decimal | None:
        delta = self.delta
        if delta is None or not self.expected_price:
            return None
        return (delta / self.expected_price * Decimal("100")).quantize(Decimal("0.01"))


@dataclass
class PriceAuditReport:
    usd_rate: Decimal = ZERO
    brp_markup: Decimal = ZERO
    polaris_markup: Decimal = ZERO
    audited: int = 0
    by_category: Counter = field(default_factory=Counter)
    by_source: Counter = field(default_factory=Counter)
    public_with_price: int = 0
    in_stock_with_price: int = 0
    public_by_category: Counter = field(default_factory=Counter)
    in_stock_by_category: Counter = field(default_factory=Counter)
    parts_with_several_sources: int = 0
    priced_without_verifiable_source: int = 0
    rows: list[PriceAuditRow] = field(default_factory=list)

    @property
    def mismatches(self) -> list[PriceAuditRow]:
        return [row for row in self.rows if row.category == PRICE_MISMATCH]

    @property
    def in_stock_mismatches(self) -> list[PriceAuditRow]:
        return [row for row in self.mismatches if row.available > ZERO]


def _classify(expected, actual, *, manual, missing_source):
    """Категория по ожидаемой и фактической цене. Без побочных эффектов.

    Ожидаемой цены нет по двум разным причинам, и путать их нельзя: источника
    может не быть вовсе (позиция снята с прайса, номера нет в прайсе), а может
    быть источник с неположительной оптовой ценой. Конвейер в обоих случаях
    сохраняет прежнюю цену, поэтому такая цена не «неправильная», а
    непроверяемая.
    """
    if expected is None:
        return WHOLESALE_SOURCE_MISSING if missing_source else SOURCE_INVALID
    if actual is None or actual <= ZERO:
        return CUSTOMER_PRICE_MISSING
    if actual == expected:
        return EXACT_MATCH
    if manual:
        return MANUAL_OVERRIDE
    # Ровно рубль - предел, который даёт другое правило округления: у 14 698,53
    # ROUND_HALF_UP даёт 14 699, а усечение 14 698. Больше рубля округлением уже
    # не объяснить, это другая цена.
    if abs(actual - expected) <= ONE_RUB:
        return ROUNDING_ONLY_MATCH
    return PRICE_MISMATCH


def _expected_from_wholesale(wholesale, usd_rate, markup):
    price = customer_price_rub(wholesale, usd_rate, markup)
    if price is None or price <= ZERO:
        return None
    return money(price)


def _availability() -> dict[int, Decimal]:
    """Доступный остаток по всем деталям, у которых он физически есть.

    Складских строк на порядки меньше, чем карточек каталога, поэтому один
    проход по ним дешевле, чем запрос остатка на каждую деталь.
    """
    totals: Counter = Counter()
    for row in live_stock_rows():
        totals[row.part_type.pk] += row.available
    return {part_id: total for part_id, total in totals.items() if total > ZERO}


def _brp_rows(usd_rate, markup, availability):
    links = BrpPartLink.objects.select_related("brp_part", "part").iterator(chunk_size=500)
    for link in links:
        part = link.part
        catalog_part = link.brp_part
        manual = link.price_source == BrpPartLink.PriceSource.MANUAL
        if not catalog_part.is_current:
            # Конвейер снимает цену с позиции, которой больше нет в прайсе.
            yield _row(
                part,
                BRP,
                catalog_part.material_no,
                None,
                ZERO,
                None,
                manual,
                availability,
                missing_source=True,
                reason="позиция снята с каталога поставщика",
            )
            continue
        source = find_brp_price_source(catalog_part.material_no_norm, catalog_part)
        wholesale = effective_wholesale_usd(source)
        surcharge = status_surcharge_usd(getattr(source, "brp_status", ""))
        expected = _expected_from_wholesale(wholesale, usd_rate, markup)
        missing = source is None or wholesale in (None, "")
        yield _row(
            part,
            BRP,
            getattr(source, "material_no", catalog_part.material_no),
            wholesale,
            surcharge,
            expected,
            manual,
            availability,
            missing_source=missing,
            reason="оптовой цены нет ни у позиции, ни в цепочке замен" if missing else "",
        )


def _polaris_rows(usd_rate, markup, availability):
    links = PolarisPartLink.objects.select_related("polaris_part", "part").iterator(chunk_size=500)
    for link in links:
        catalog_part = link.polaris_part
        source = find_polaris_price_source(catalog_part.part_number_norm, catalog_part)
        wholesale = getattr(source, "wholesale_price_usd", None)
        expected = _expected_from_wholesale(wholesale, usd_rate, markup)
        missing = source is None or wholesale in (None, "")
        yield _row(
            link.part,
            POLARIS,
            getattr(source, "part_number", catalog_part.part_number),
            wholesale,
            ZERO,
            expected,
            link.price_source == PolarisPartLink.PriceSource.MANUAL,
            availability,
            missing_source=missing,
            reason="номера нет в текущем прайсе Polaris" if missing else "",
        )


def _aftermarket_rows(usd_rate, markup, availability):
    from apps.catalog_import.models import AftermarketCatalogPart

    rows = (
        AftermarketCatalogPart.objects.values_list(
            "part_id",
            "manufacturer_number",
            "dealer_cost_usd",
            "part__recommended_price",
            "part__is_public",
            "part__is_active",
        )
        .order_by("part_id")
        .iterator(chunk_size=5000)
    )
    for part_id, article, dealer_cost, actual, is_public, is_active in rows:
        expected = _expected_from_wholesale(dealer_cost, usd_rate, markup)
        missing = dealer_cost in (None, "")
        reason = "у карточки каталога нет дилерской цены" if missing else ""
        yield PriceAuditRow(
            part_id=part_id,
            # У карточки каталога аналогов нет поля происхождения цены, поэтому
            # ручной цены у неё не бывает: владелец цены - импортёр.
            category=_classify(expected, actual, manual=False, missing_source=missing),
            source=AFTERMARKET,
            source_reference=article or "",
            wholesale_usd=dealer_cost,
            surcharge_usd=ZERO,
            expected_price=expected,
            actual_price=actual,
            manual=False,
            is_public=bool(is_public and is_active),
            available=availability.get(part_id, ZERO),
            reason=reason,
            article=article or "",
        )


def _row(
    part,
    source,
    reference,
    wholesale,
    surcharge,
    expected,
    manual,
    availability,
    *,
    missing_source,
    reason,
):
    return PriceAuditRow(
        part_id=part.pk,
        category=_classify(
            expected, part.recommended_price, manual=manual, missing_source=missing_source
        ),
        source=source,
        source_reference=reference or "",
        wholesale_usd=wholesale,
        surcharge_usd=surcharge,
        expected_price=expected,
        actual_price=part.recommended_price,
        manual=manual,
        is_public=bool(part.is_public and part.is_active),
        available=availability.get(part.pk, ZERO),
        reason=reason,
    )


def _unlinked_rows(seen_ids, availability):
    """Карточки без связи с каталогом: оптовой цены у них нет в принципе."""
    rows = (
        PartType.objects.exclude(pk__in=seen_ids)
        .values_list("pk", "name", "recommended_price", "is_public", "is_active")
        .order_by("pk")
        .iterator(chunk_size=5000)
    )
    for part_id, name, actual, is_public, is_active in rows:
        yield PriceAuditRow(
            part_id=part_id,
            category=FORMULA_NOT_APPLICABLE,
            source=NO_SOURCE,
            source_reference="",
            wholesale_usd=None,
            surcharge_usd=ZERO,
            expected_price=None,
            actual_price=actual,
            manual=False,
            is_public=bool(is_public and is_active),
            available=availability.get(part_id, ZERO),
            reason="карточка не связана с каталогом поставщика",
            name=name,
        )


# Цену этих категорий сверить нечем: источника нет, он неположительный или
# карточка вообще не из каталога поставщика.
UNVERIFIABLE = frozenset({WHOLESALE_SOURCE_MISSING, SOURCE_INVALID, FORMULA_NOT_APPLICABLE})

KEEP_CATEGORIES = frozenset(
    {PRICE_MISMATCH, MANUAL_OVERRIDE, ROUNDING_ONLY_MATCH, CUSTOMER_PRICE_MISSING, SOURCE_INVALID}
)


def audit_prices(
    *,
    usd_rate: Decimal,
    brp_markup: Decimal,
    polaris_markup: Decimal,
    keep_rows: frozenset[str] = KEEP_CATEGORIES,
    keep_priced_without_source: bool = True,
) -> PriceAuditReport:
    """Сверить клиентскую цену каждой детали с оптовой ценой её каталога.

    Подробности собираются только по интересным категориям: карточек каталога
    больше ста тысяч, и держать в памяти строку на каждую незачем. Счётчики
    считаются по всем.
    """
    report = PriceAuditReport(
        usd_rate=usd_rate, brp_markup=brp_markup, polaris_markup=polaris_markup
    )
    availability = _availability()
    seen: dict[int, str] = {}

    streams = (
        _brp_rows(usd_rate, brp_markup, availability),
        _polaris_rows(usd_rate, polaris_markup, availability),
        _aftermarket_rows(usd_rate, brp_markup, availability),
    )
    for stream in streams:
        for row in stream:
            if row.part_id in seen:
                report.parts_with_several_sources += 1
            seen[row.part_id] = row.source
            _account(report, row, keep_rows, keep_priced_without_source)

    for row in _unlinked_rows(set(seen), availability):
        _account(report, row, keep_rows, keep_priced_without_source)

    _hydrate(report)
    return report


def _account(report, row, keep_rows, keep_priced_without_source):
    report.audited += 1
    report.by_category[row.category] += 1
    report.by_source[row.source] += 1
    priced = row.actual_price is not None and row.actual_price > ZERO
    if row.is_public and priced:
        report.public_with_price += 1
        report.public_by_category[row.category] += 1
        if row.available > ZERO:
            report.in_stock_with_price += 1
            report.in_stock_by_category[row.category] += 1
    unverifiable = row.category in UNVERIFIABLE
    if priced and unverifiable:
        report.priced_without_verifiable_source += 1
    keep = row.category in keep_rows or (
        keep_priced_without_source and priced and unverifiable and row.is_public
    )
    if keep:
        report.rows.append(row)


def _hydrate(report: PriceAuditReport) -> None:
    """Дозаполнить артикул, название и производителя только у строк отчёта."""
    ids = [row.part_id for row in report.rows]
    if not ids:
        return
    identity = {}
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start : chunk_start + 500]
        for part in with_part_identity(PartType.objects.filter(pk__in=chunk), part_field=""):
            identity[part.pk] = (
                part_exact_number(part, default=""),
                part.name,
                manufacturer_display(part),
            )
    filled = []
    for row in report.rows:
        article, name, manufacturer = identity.get(row.part_id, ("", "", ""))
        filled.append(
            replace(
                row,
                article=article or row.article,
                name=name or row.name,
                manufacturer=manufacturer or row.manufacturer,
            )
        )
    report.rows = filled
