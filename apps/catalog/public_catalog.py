"""Public catalog read service: visibility, filters, facets and result cards.

Views stay orchestrators. This module turns Search 2.0 identities into the
customer-facing result list without adding a business rule of its own:

* ranking comes only from ``apps.catalog.search``;
* price, availability, article, manufacturer and the confirmed Russian name
  come only from ``build_public_part_facts`` and the facades behind it;
* an "Оригинал"/"Аналог" label and filter come only from ``PartAnalog`` rows a
  catalog manager confirmed, with both parts publicly visible;
* the application filter reads only explicit operator data: the customs
  application area chosen by a person, or explicit vehicle compatibility read
  through the same table the customs export uses.

Every query is bounded by Search 2.0's ``RESULT_CAP`` identities and none is
issued per result row, so the query count does not grow with the page size.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Q

from apps.actions.models import PartCustomsInfo
from apps.inventory.availability import available_totals
from apps.inventory.presentation import manufacturer_display

from .models import PartAnalog, PartCompatibility, PartType
from .public_contracts import PublicPartFacts, build_public_part_facts
from .public_photos import PublicPhotoRef, primary_photos
from .search import RESULT_CAP, clean_query, search_part_ids

ZERO = Decimal("0")
PAGE_SIZE = 20
# The approved public application values. They are the operator's own
# customs categories, so the filter never invents a vocabulary.
APPLICATIONS = (
    PartCustomsInfo.ApplicationArea.WATERCRAFT.value,
    PartCustomsInfo.ApplicationArea.ATV.value,
    PartCustomsInfo.ApplicationArea.SNOWMOBILE.value,
    PartCustomsInfo.ApplicationArea.OUTBOARD_MOTOR.value,
    PartCustomsInfo.ApplicationArea.BOAT_CRAFT.value,
)
APPLICATION_LABELS = {
    value: label
    for value, label in PartCustomsInfo.ApplicationArea.choices
    if value in APPLICATIONS
}
RELATION_ORIGINAL = "original"
RELATION_ANALOG = "analog"
RELATION_LABELS = {
    RELATION_ORIGINAL: "Оригиналы с аналогами",
    RELATION_ANALOG: "Аналоги",
}
MAX_MANUFACTURER_LENGTH = 150
FILTER_PARAMS = ("application", "manufacturer", "relation", "in_stock")


def public_parts():
    """The one visibility rule: published and not retired from the catalog."""
    return PartType.objects.filter(is_public=True, is_active=True)


# --- Request parameters ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogFilters:
    """Validated filter state. Unknown values are dropped, never trusted."""

    application: str = ""
    manufacturer: str = ""
    relation: str = ""
    in_stock: bool = False

    @classmethod
    def from_params(cls, params: Mapping) -> CatalogFilters:
        application = str(params.get("application") or "").strip()
        manufacturer = " ".join(str(params.get("manufacturer") or "").split())
        relation = str(params.get("relation") or "").strip()
        return cls(
            application=application if application in APPLICATIONS else "",
            manufacturer=manufacturer[:MAX_MANUFACTURER_LENGTH],
            relation=relation if relation in RELATION_LABELS else "",
            in_stock=str(params.get("in_stock") or "") == "1",
        )

    def as_params(self) -> dict[str, str]:
        params = {
            "application": self.application,
            "manufacturer": self.manufacturer,
            "relation": self.relation,
            "in_stock": "1" if self.in_stock else "",
        }
        return {key: value for key, value in params.items() if value}

    @property
    def active_count(self) -> int:
        return len(self.as_params())


def parse_page(raw) -> int:
    """A page number from the query string; anything malformed is page 1."""
    text = str(raw or "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 6:
        return 1
    return max(1, int(text))


# --- Per-identity attributes (bounded, batched) ---------------------------------


@dataclass(slots=True)
class _Attributes:
    manufacturer: str = ""
    applications: frozenset[str] = frozenset()
    is_original: bool = False
    is_analog: bool = False
    available: Decimal = ZERO


def _visible_with_manufacturer(part_ids: list[int]) -> dict[int, str]:
    """Visible IDs mapped to the canonical manufacturer label, in one query.

    ``manufacturer_display`` is the only place that decides the label (BRP,
    then Polaris catalog link, then the card's manufacturer), so the filter
    and the card can never disagree.
    """
    if not part_ids:
        return {}
    parts = (
        public_parts()
        .filter(pk__in=part_ids)
        .select_related("brp_link", "polaris_link", "manufacturer")
        .only("pk", "manufacturer__name", "brp_link__id", "polaris_link__id")
    )
    return {part.pk: manufacturer_display(part) for part in parts}


def _applications(part_ids: list[int]) -> dict[int, set[str]]:
    """Approved applications per part from explicit operator data only."""
    from apps.actions.services import application_area_for_vehicle_type

    found: dict[int, set[str]] = {}
    explicit = PartCustomsInfo.objects.filter(part_type_id__in=part_ids).exclude(
        application_area=""
    )
    for part_id, area in explicit.values_list("part_type_id", "application_area"):
        value = (area or "").strip().upper()
        if value in APPLICATIONS:
            found.setdefault(part_id, set()).add(value)
    compatible = PartCompatibility.objects.filter(part_id__in=part_ids).values_list(
        "part_id", "vehicle_model__vehicle_make__vehicle_type__name"
    )
    for part_id, vehicle_type in compatible.distinct():
        value = application_area_for_vehicle_type(vehicle_type)
        if value in APPLICATIONS:
            found.setdefault(part_id, set()).add(value)
    return found


def confirmed_links():
    """Analog links a person confirmed, with both sides publicly visible."""
    return PartAnalog.objects.filter(
        is_confirmed=True,
        original__is_public=True,
        original__is_active=True,
        analog__is_public=True,
        analog__is_active=True,
    )


def _relations(part_ids: list[int]) -> tuple[set[int], set[int]]:
    """Which parts are the original side and which the analog side of a link."""
    originals: set[int] = set()
    analogs: set[int] = set()
    links = confirmed_links().filter(Q(original_id__in=part_ids) | Q(analog_id__in=part_ids))
    wanted = set(part_ids)
    for original_id, analog_id in links.values_list("original_id", "analog_id"):
        if original_id in wanted:
            originals.add(original_id)
        if analog_id in wanted:
            analogs.add(analog_id)
    return originals, analogs


def _attributes(part_ids: list[int]) -> tuple[list[int], dict[int, _Attributes]]:
    """Visible IDs in rank order plus everything the filters need about them."""
    manufacturers = _visible_with_manufacturer(part_ids)
    visible = [part_id for part_id in part_ids if part_id in manufacturers]
    if not visible:
        return [], {}
    applications = _applications(visible)
    originals, analogs = _relations(visible)
    quantities = available_totals(visible)
    return visible, {
        part_id: _Attributes(
            manufacturer=manufacturers[part_id],
            applications=frozenset(applications.get(part_id, ())),
            is_original=part_id in originals,
            is_analog=part_id in analogs,
            available=quantities[part_id],
        )
        for part_id in visible
    }


# --- Filtering and facets --------------------------------------------------------


def _passes(attrs: _Attributes, filters: CatalogFilters, *, skip: str = "") -> bool:
    if skip != "application" and filters.application:
        if filters.application not in attrs.applications:
            return False
    if skip != "manufacturer" and filters.manufacturer:
        if attrs.manufacturer.casefold() != filters.manufacturer.casefold():
            return False
    if skip != "relation" and filters.relation:
        if filters.relation == RELATION_ORIGINAL and not attrs.is_original:
            return False
        if filters.relation == RELATION_ANALOG and not attrs.is_analog:
            return False
    if skip != "in_stock" and filters.in_stock and attrs.available <= ZERO:
        return False
    return True


@dataclass(frozen=True, slots=True)
class FacetOption:
    value: str
    label: str
    count: int
    selected: bool


@dataclass(slots=True)
class Facets:
    applications: list[FacetOption] = field(default_factory=list)
    manufacturers: list[FacetOption] = field(default_factory=list)
    relations: list[FacetOption] = field(default_factory=list)
    in_stock_count: int = 0

    @property
    def has_relations(self) -> bool:
        return any(option.count or option.selected for option in self.relations)

    @property
    def has_applications(self) -> bool:
        return any(option.count or option.selected for option in self.applications)


def _facets(attributes: dict[int, _Attributes], filters: CatalogFilters) -> Facets:
    """Counts per option with every OTHER active filter applied."""
    by_application = dict.fromkeys(APPLICATIONS, 0)
    by_manufacturer: dict[str, int] = {}
    by_relation = dict.fromkeys(RELATION_LABELS, 0)
    in_stock = 0
    for attrs in attributes.values():
        if _passes(attrs, filters, skip="application"):
            for value in attrs.applications:
                by_application[value] += 1
        if _passes(attrs, filters, skip="manufacturer") and attrs.manufacturer:
            by_manufacturer[attrs.manufacturer] = by_manufacturer.get(attrs.manufacturer, 0) + 1
        if _passes(attrs, filters, skip="relation"):
            by_relation[RELATION_ORIGINAL] += attrs.is_original
            by_relation[RELATION_ANALOG] += attrs.is_analog
        if _passes(attrs, filters, skip="in_stock") and attrs.available > ZERO:
            in_stock += 1

    selected_manufacturer = filters.manufacturer.casefold()
    manufacturers = [
        FacetOption(name, name, count, name.casefold() == selected_manufacturer)
        for name, count in sorted(by_manufacturer.items(), key=lambda item: item[0].casefold())
    ]
    if filters.manufacturer and not any(option.selected for option in manufacturers):
        # A shared link can name a manufacturer absent from these results. It
        # stays visible and selected so the customer can see and clear it.
        manufacturers.insert(
            0, FacetOption(filters.manufacturer, filters.manufacturer, 0, True)
        )
    return Facets(
        applications=[
            FacetOption(value, APPLICATION_LABELS[value], count, value == filters.application)
            for value, count in by_application.items()
        ],
        manufacturers=manufacturers,
        relations=[
            FacetOption(value, RELATION_LABELS[value], count, value == filters.relation)
            for value, count in by_relation.items()
        ],
        in_stock_count=in_stock,
    )


# --- Cards --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartCard:
    """What a result, analog or cart row shows about one public part."""

    facts: PublicPartFacts
    is_original: bool = False
    is_analog: bool = False
    photo: PublicPhotoRef | None = None

    @property
    def display_name(self) -> str:
        return self.facts.russian_name or self.facts.english_name

    @property
    def in_stock(self) -> bool:
        return self.facts.available_quantity > ZERO


def cards_by_id(
    part_ids: Iterable[int],
    *,
    quantities: Mapping[int, Decimal] | None = None,
    relations: tuple[set[int], set[int]] | None = None,
) -> dict[int, PartCard]:
    """Hydrate cards for already-visible part IDs, keyed by warehouse ID.

    The key stays server-side: templates receive only the card, whose
    identity is the opaque ``public_id``.
    """
    ids = list(dict.fromkeys(part_ids))
    if not ids:
        return {}
    public_ids = dict(PartType.objects.filter(pk__in=ids).values_list("pk", "public_id"))
    facts_by_public_id = {
        fact.public_id: fact for fact in build_public_part_facts(ids, quantities=quantities)
    }
    originals, analogs = relations if relations is not None else _relations(ids)
    photos = primary_photos(ids)
    cards = {}
    for part_id in ids:
        fact = facts_by_public_id.get(public_ids.get(part_id))
        if fact is not None:
            cards[part_id] = PartCard(
                facts=fact,
                is_original=part_id in originals,
                is_analog=part_id in analogs,
                photo=photos.get(part_id),
            )
    return cards


def build_cards(part_ids: Iterable[int], **kwargs) -> list[PartCard]:
    """Cards in the requested order; IDs without a public part are skipped."""
    ids = list(dict.fromkeys(part_ids))
    by_id = cards_by_id(ids, **kwargs)
    return [by_id[part_id] for part_id in ids if part_id in by_id]


# --- Search page ---------------------------------------------------------------------


@dataclass(slots=True)
class CatalogPage:
    query: str
    filters: CatalogFilters
    cards: list[PartCard]
    facets: Facets
    total: int
    ranked_total: int
    page: int
    pages: int
    truncated: bool

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_index(self) -> int:
        return (self.page - 1) * PAGE_SIZE + 1 if self.total else 0

    @property
    def last_index(self) -> int:
        return min(self.page * PAGE_SIZE, self.total)

    def url(self, *, page: int | None = None, drop: str = "", reset: bool = False) -> str:
        """A shareable search URL for this state, optionally changed."""
        params: dict[str, str] = {"q": self.query}
        if not reset:
            params.update(self.filters.as_params())
            params.pop(drop, None)
        if page and page > 1:
            params["page"] = str(page)
        return "?" + urlencode(params)


def search_catalog(raw_query, params: Mapping, *, page_size: int = PAGE_SIZE) -> CatalogPage:
    """Ranked, filtered and paginated public results for one request.

    Filters work on the whole ranked list (up to ``RESULT_CAP``), not on one
    page of it, so the total, the facets and every page agree. Only the page
    being shown is hydrated.
    """
    query = clean_query(raw_query)
    filters = CatalogFilters.from_params(params)
    ranked = [hit.part_id for hit in search_part_ids(query, limit=RESULT_CAP)] if query else []
    visible, attributes = _attributes(ranked)
    matched = [part_id for part_id in visible if _passes(attributes[part_id], filters)]
    pages = max(1, math.ceil(len(matched) / page_size))
    page = min(parse_page(params.get("page")), pages)
    window = matched[(page - 1) * page_size : page * page_size]
    cards = build_cards(
        window,
        quantities={part_id: attributes[part_id].available for part_id in window},
        relations=(
            {part_id for part_id in window if attributes[part_id].is_original},
            {part_id for part_id in window if attributes[part_id].is_analog},
        ),
    )
    return CatalogPage(
        query=query,
        filters=filters,
        cards=cards,
        facets=_facets(attributes, filters),
        total=len(matched),
        ranked_total=len(visible),
        page=page,
        pages=pages,
        truncated=len(ranked) >= RESULT_CAP,
    )


# --- Part detail ----------------------------------------------------------------------


@dataclass(slots=True)
class PartRelations:
    """Confirmed relations of one part, both directions, as public cards."""

    analogs: list[PartCard]
    originals: list[PartCard]


def part_relations(part_id: int) -> PartRelations:
    links = list(
        confirmed_links()
        .filter(Q(original_id=part_id) | Q(analog_id=part_id))
        .order_by("pk")
        .values_list("original_id", "analog_id")
    )
    analog_ids = [analog_id for original_id, analog_id in links if original_id == part_id]
    original_ids = [original_id for original_id, analog_id in links if analog_id == part_id]
    cards = cards_by_id([*analog_ids, *original_ids])
    return PartRelations(
        analogs=[cards[other] for other in dict.fromkeys(analog_ids) if other in cards],
        originals=[cards[other] for other in dict.fromkeys(original_ids) if other in cards],
    )
