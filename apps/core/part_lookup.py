"""Canonical read-only lookup for warehouse part identity and live stock."""

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from apps.catalog.models import (
    PartBarcode,
    PartCompatibility,
    PartNumber,
    PartType,
    normalize_number,
)
from apps.inventory.models import PartItem, StockBalance
from apps.inventory.movement import LiveStockRow, live_stock_rows
from apps.inventory.presentation import (
    EXACT_NUMBER_KINDS,
    analog_numbers_prefetch,
    manufacturer_display,
    part_exact_number,
    with_part_identity,
)

DEC0 = Decimal("0")
RESULT_LIMIT = 30


class MatchSource:
    EXACT = "exact_number"
    BARCODE = "barcode"
    REPLACEMENT = "replacement"
    SUPERSEDED = "superseded"
    ALIAS = "alias"
    INTERNAL = "internal_number"
    SERIAL = "serial_number"
    NUMBER_PARTIAL = "number_partial"
    NAME = "name"
    COMPATIBILITY = "compatibility"


SOURCE_LABELS = {
    MatchSource.EXACT: "точному артикулу",
    MatchSource.BARCODE: "штрихкоду",
    MatchSource.REPLACEMENT: "заменённому номеру",
    MatchSource.SUPERSEDED: "superseded номеру",
    MatchSource.ALIAS: "вспомогательному номеру",
    MatchSource.INTERNAL: "внутреннему номеру",
    MatchSource.SERIAL: "серийному номеру",
    MatchSource.NUMBER_PARTIAL: "части артикула",
    MatchSource.NAME: "названию",
    MatchSource.COMPATIBILITY: "совместимости",
}


def clean_lookup_value(raw) -> str:
    """Normalize keyboard/scanner framing without changing meaningful content."""
    return str(raw or "").replace("\r", "").replace("\n", "").strip()


@dataclass
class PartLookupCandidate:
    part: PartType
    exact_number: str
    manufacturer: str
    category: str
    barcodes: list[str]
    match_source: str
    matched_value: str
    is_alias: bool
    locations: list[str] = field(default_factory=list)
    location_rows: list[LiveStockRow] = field(default_factory=list)
    physical: Decimal = DEC0
    available: Decimal = DEC0
    reserved: Decimal = DEC0
    quarantine: Decimal = DEC0
    receiving: Decimal = DEC0
    batches: list[str] = field(default_factory=list)
    client_price: Decimal | None = None
    analogs: list[str] = field(default_factory=list)
    items: list = field(default_factory=list)
    lots: list = field(default_factory=list)
    source: str = "primary"

    @property
    def match_label(self) -> str:
        return SOURCE_LABELS.get(self.match_source, self.match_source)

    @property
    def alias_message(self) -> str:
        if self.match_source == MatchSource.REPLACEMENT:
            return f"Найдено по заменённому номеру: {self.matched_value}"
        if self.match_source == MatchSource.SUPERSEDED:
            return f"Найдено по superseded номеру: {self.matched_value}"
        if self.is_alias:
            return f"Найдено по вспомогательному номеру: {self.matched_value}"
        return ""


@dataclass
class PartLookupResult:
    query: str
    normalized: str
    status: str
    candidates: list[PartLookupCandidate] = field(default_factory=list)
    message: str = ""

    @property
    def found(self) -> bool:
        return self.status == "found" and len(self.candidates) == 1

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"

    @property
    def candidate(self) -> PartLookupCandidate | None:
        return self.candidates[0] if self.found else None


def _strong_match(norm: str, raw: str):
    if norm:
        exact_ids = list(
            PartNumber.objects.filter(
                normalized_value=norm, kind__in=EXACT_NUMBER_KINDS
            )
            .values_list("part_id", flat=True)
            .distinct()[: RESULT_LIMIT + 1]
        )
        if exact_ids:
            alias_ids = list(
                PartNumber.objects.filter(
                    normalized_value=norm, kind=PartNumber.Kind.ANALOG
                )
                .values_list("part_id", flat=True)
                .distinct()[: RESULT_LIMIT + 1]
            )
            sources = {part_id: MatchSource.ALIAS for part_id in alias_ids}
            sources.update({part_id: MatchSource.EXACT for part_id in exact_ids})
            return list(sources), sources, raw, len(sources) > 1

    barcode_ids = list(
        PartBarcode.objects.filter(value__iexact=raw)
        .values_list("part_id", flat=True)
        .distinct()[: RESULT_LIMIT + 1]
    )
    if barcode_ids:
        sources = {part_id: MatchSource.BARCODE for part_id in barcode_ids}
        return barcode_ids, sources, raw, len(barcode_ids) > 1

    if norm:
        alias_ids = list(
            PartNumber.objects.filter(
                normalized_value=norm, kind=PartNumber.Kind.ANALOG
            )
            .values_list("part_id", flat=True)
            .distinct()[: RESULT_LIMIT + 1]
        )
        if alias_ids:
            sources = {part_id: MatchSource.ALIAS for part_id in alias_ids}
            return alias_ids, sources, raw, len(alias_ids) > 1
    return None


def _secondary_match(norm: str, raw: str, *, allow_partial: bool, allow_name: bool):
    item_q = Q(internal_number__iexact=raw) | Q(internal_barcode__iexact=raw)
    item_ids = list(
        PartItem.objects.filter(item_q)
        .values_list("part_type_id", flat=True)
        .distinct()[: RESULT_LIMIT + 1]
    )
    if item_ids:
        return item_ids, MatchSource.INTERNAL, raw

    serial_ids = list(
        PartItem.objects.filter(serial_number__iexact=raw)
        .values_list("part_type_id", flat=True)
        .distinct()[: RESULT_LIMIT + 1]
    )
    if serial_ids:
        return serial_ids, MatchSource.SERIAL, raw

    if allow_partial and norm:
        partial_ids = list(
            PartNumber.objects.filter(normalized_value__icontains=norm)
            .values_list("part_id", flat=True)
            .distinct()[:RESULT_LIMIT]
        )
        if partial_ids:
            return partial_ids, MatchSource.NUMBER_PARTIAL, raw

    if allow_name:
        name_ids = list(
            PartType.objects.filter(name__icontains=raw)
            .values_list("pk", flat=True)[:RESULT_LIMIT]
        )
        if name_ids:
            return name_ids, MatchSource.NAME, raw
        compatibility_ids = list(
            PartCompatibility.objects.filter(
                Q(vehicle_model__name__icontains=raw)
                | Q(vehicle_model__vehicle_make__name__icontains=raw)
            )
            .values_list("part_id", flat=True)
            .distinct()[:RESULT_LIMIT]
        )
        if compatibility_ids:
            return compatibility_ids, MatchSource.COMPATIBILITY, raw
    return None


def _specific_alias_source(part: PartType, norm: str) -> str:
    try:
        brp = part.brp_link.brp_part
    except (AttributeError, ObjectDoesNotExist):
        brp = None
    if brp is not None and norm in {
        brp.replacement_no_1_norm,
        brp.replacement_no_2_norm,
    }:
        return MatchSource.REPLACEMENT
    try:
        polaris = part.polaris_link.polaris_part
    except (AttributeError, ObjectDoesNotExist):
        polaris = None
    if polaris is not None and norm == polaris.superseded_number_norm:
        return MatchSource.SUPERSEDED
    return MatchSource.ALIAS


def _parts_for_ids(part_ids) -> list[PartType]:
    return list(
        with_part_identity(
            PartType.objects.filter(pk__in=part_ids).select_related("category", "unit"),
            part_field="",
        )
        .prefetch_related(analog_numbers_prefetch(), "barcodes")
        .order_by("name", "pk")
    )


def _candidates(
    part_ids,
    *,
    source: str | dict[int, str],
    matched_value: str,
    norm: str,
    include_price: bool,
) -> list[PartLookupCandidate]:
    parts = _parts_for_ids(part_ids)
    part_pks = [part.pk for part in parts]
    balance_part_ids = set(
        StockBalance.objects.filter(part_type_id__in=part_pks)
        .values_list("part_type_id", flat=True)
        .distinct()
    )
    stock_by_part: dict[int, list[LiveStockRow]] = {}
    for row in live_stock_rows(part_ids=part_pks):
        stock_by_part.setdefault(row.part_type.pk, []).append(row)

    result = []
    for part in parts:
        locations = stock_by_part.get(part.pk, [])
        candidate_source = (
            source.get(part.pk, MatchSource.ALIAS)
            if isinstance(source, dict)
            else source
        )
        actual_source = (
            _specific_alias_source(part, norm)
            if candidate_source == MatchSource.ALIAS
            else candidate_source
        )
        result.append(
            PartLookupCandidate(
                part=part,
                exact_number=part_exact_number(part, default=""),
                manufacturer=manufacturer_display(part),
                category=part.category.name,
                barcodes=[barcode.value for barcode in part.barcodes.all()],
                match_source=actual_source,
                matched_value=matched_value,
                is_alias=actual_source in {
                    MatchSource.ALIAS,
                    MatchSource.REPLACEMENT,
                    MatchSource.SUPERSEDED,
                },
                locations=[row.location.code for row in locations],
                location_rows=locations,
                physical=sum((row.physical for row in locations), DEC0),
                available=sum((row.available for row in locations), DEC0),
                reserved=sum((row.reserved for row in locations), DEC0),
                quarantine=sum((row.quarantine for row in locations), DEC0),
                receiving=sum((row.receiving for row in locations), DEC0),
                batches=sorted({batch for row in locations for batch in row.batches}),
                client_price=part.recommended_price if include_price else None,
                analogs=[number.value for number in part.analog_numbers_for_display],
                source="balance" if part.pk in balance_part_ids else "primary",
            )
        )
    return result


def resolve_part_lookup(
    raw,
    *,
    allow_partial: bool = False,
    allow_name: bool = False,
    include_price: bool = False,
) -> PartLookupResult:
    query = clean_lookup_value(raw)
    norm = normalize_number(query)
    if not query:
        return PartLookupResult(query, norm, "not_found", message="Пустой запрос.")

    matched = _strong_match(norm, query)
    strong = matched is not None
    if matched is None:
        matched = _secondary_match(
            norm, query, allow_partial=allow_partial, allow_name=allow_name
        )
    if matched is None:
        return PartLookupResult(query, norm, "not_found", message="Деталь не найдена.")

    part_ids, source, matched_value, *rest = matched
    ambiguous = bool(rest[0]) if rest else False
    candidates = _candidates(
        part_ids,
        source=source,
        matched_value=matched_value,
        norm=norm,
        include_price=include_price,
    )
    if strong and ambiguous:
        return PartLookupResult(
            query,
            norm,
            "ambiguous",
            candidates,
            "Найдено несколько складских карточек. Выберите точную деталь.",
        )
    status = "found" if len(candidates) == 1 else "multiple"
    return PartLookupResult(query, norm, status, candidates)


def lookup_part_by_id(part, *, include_price: bool = False) -> PartLookupCandidate:
    return _candidates(
        [part.pk],
        source=MatchSource.EXACT,
        matched_value=part_exact_number(part, default=""),
        norm="",
        include_price=include_price,
    )[0]
