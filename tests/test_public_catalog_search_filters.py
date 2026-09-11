"""Public search results: visibility, composable filters, facets and paging.

Filters run over the whole Search 2.0 ranked list, not over one page of it, so
totals, facets and every page agree. The original/analog filter reads only
confirmed ``PartAnalog`` rows between public parts.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from django.db import connection

from apps.catalog.models import PartCompatibility, VehicleMake, VehicleModel, VehicleType
from apps.catalog.public_catalog import (
    PAGE_SIZE,
    CatalogFilters,
    parse_page,
    search_catalog,
)
from tests.public_catalog_support import assert_no_writes, capture


# Search 2.0 issues up to 9 SELECTs on SQLite; on PostgreSQL inside a test
# transaction its fuzzy tier adds a savepoint, the threshold read/restore and
# the fuzzy query. The budget is fixed: it must not grow with the result size.
_QUERY_BUDGET = {"sqlite": 26, "postgresql": 32}


def _ids(result):
    return [card.facts.public_id for card in result.cards]


# --- Visibility --------------------------------------------------------------------


def test_search_hides_non_public_and_retired_parts(public_catalog):
    shown = public_catalog.part("Visible gasket", article="VIS-001")
    hidden = public_catalog.part("Hidden gasket", article="VIS-002", public=False)
    retired = public_catalog.part("Retired gasket", article="VIS-003", active=False)

    result = search_catalog("gasket", {})

    assert _ids(result) == [shown.public_id]
    assert hidden.public_id not in _ids(result)
    assert retired.public_id not in _ids(result)
    assert result.total == 1


def test_search_page_never_links_a_hidden_part(public_client, public_catalog):
    hidden = public_catalog.part("Hidden bearing", article="HID-1", public=False)
    body = public_client.get("/search/", {"q": "HID-1"}).content.decode()
    assert str(hidden.public_id) not in body
    assert "ничего не нашлось" in body


# --- Filters compose with Search 2.0 and with each other -----------------------------


def test_manufacturer_filter_uses_the_canonical_display_label(public_catalog):
    wiseco = public_catalog.part("Piston kit A", article="PK-A", maker="WISECO")
    public_catalog.part("Piston kit B", article="PK-B", maker="VERTEX")

    result = search_catalog("piston kit", {"manufacturer": "wiseco"})

    assert _ids(result) == [wiseco.public_id]
    options = {option.value: option for option in result.facets.manufacturers}
    assert options["WISECO"].selected and options["WISECO"].count == 1
    # The facet for the selected dimension ignores its own filter.
    assert options["VERTEX"].count == 1


def test_in_stock_filter_uses_canonical_availability(public_catalog):
    stocked = public_catalog.part("Oil filter stocked", article="OF-1")
    public_catalog.part("Oil filter empty", article="OF-2")
    public_catalog.stock(stocked, "2")

    result = search_catalog("oil filter", {"in_stock": "1"})

    assert _ids(result) == [stocked.public_id]
    assert result.facets.in_stock_count == 1
    assert result.ranked_total == 2


def test_application_filter_reads_explicit_area_and_compatibility_only(public_catalog):
    by_area = public_catalog.part("Impeller area", article="IMP-1", application="ГИДРОЦИКЛ")
    by_compat = public_catalog.part("Impeller compat", article="IMP-2")
    public_catalog.part("Impeller unknown", article="IMP-3")
    watercraft = VehicleType.objects.get_or_create(name="Гидроцикл")[0]
    model = VehicleModel.objects.create(
        vehicle_make=VehicleMake.objects.create(vehicle_type=watercraft, name="Sea-Doo"),
        name="RXP-X 300",
    )
    PartCompatibility.objects.create(part=by_compat, vehicle_model=model)

    result = search_catalog("impeller", {"application": "ГИДРОЦИКЛ"})

    assert set(_ids(result)) == {by_area.public_id, by_compat.public_id}
    counts = {option.value: option.count for option in result.facets.applications}
    assert counts["ГИДРОЦИКЛ"] == 2
    assert counts["СНЕГОХОД"] == 0


def test_unknown_filter_values_are_ignored_not_trusted(public_catalog):
    part = public_catalog.part("Clutch spring", article="CS-1")

    filters = CatalogFilters.from_params(
        {"application": "МОТОЦИКЛ", "relation": "guess", "in_stock": "yes"}
    )
    assert filters == CatalogFilters()
    assert _ids(search_catalog("clutch", {"application": "'; DROP TABLE"})) == [part.public_id]


def test_filters_compose_and_the_total_reflects_all_of_them(public_catalog):
    matching = public_catalog.part(
        "Brake pad match", article="BP-1", maker="EBC", application="КВАДРОЦИКЛ"
    )
    public_catalog.part(
        "Brake pad other maker", article="BP-2", maker="SBS", application="КВАДРОЦИКЛ"
    )
    public_catalog.part("Brake pad no stock", article="BP-3", maker="EBC", application="КВАДРОЦИКЛ")
    public_catalog.part("Brake pad snow", article="BP-4", maker="EBC", application="СНЕГОХОД")
    public_catalog.stock(matching, "1")
    other = public_catalog.part("Brake pad stocked other", article="BP-5", maker="SBS")
    public_catalog.stock(other, "1")

    result = search_catalog(
        "brake pad", {"manufacturer": "EBC", "application": "КВАДРОЦИКЛ", "in_stock": "1"}
    )

    assert _ids(result) == [matching.public_id]
    assert result.total == 1
    assert result.ranked_total == 5


# --- Confirmed analog filter ------------------------------------------------------------


def test_relation_filter_reads_only_confirmed_links_between_public_parts(public_catalog):
    original = public_catalog.part("Water pump OEM", article="WP-OEM")
    confirmed = public_catalog.part("Water pump aftermarket", article="WP-AM1")
    unconfirmed = public_catalog.part("Water pump guess", article="WP-AM2")
    hidden_original = public_catalog.part("Water pump hidden OEM", article="WP-OEM2", public=False)
    orphan = public_catalog.part("Water pump orphan analog", article="WP-AM3")
    public_catalog.analog(original, confirmed)
    public_catalog.analog(original, unconfirmed, confirmed=False)
    public_catalog.analog(hidden_original, orphan)

    analogs = search_catalog("water pump", {"relation": "analog"})
    originals = search_catalog("water pump", {"relation": "original"})

    assert _ids(analogs) == [confirmed.public_id]
    assert _ids(originals) == [original.public_id]
    # A relation to a non-public part is not a public relation.
    assert orphan.public_id not in _ids(analogs)
    labels = {card.facts.public_id: card for card in search_catalog("water pump", {}).cards}
    assert labels[original.public_id].is_original
    assert labels[confirmed.public_id].is_analog
    assert not labels[unconfirmed.public_id].is_analog
    assert not labels[orphan.public_id].is_analog


def test_revoking_confirmation_removes_the_public_label(public_catalog):
    original = public_catalog.part("Fuel pump OEM", article="FP-OEM")
    analog = public_catalog.part("Fuel pump analog", article="FP-AM")
    link = public_catalog.analog(original, analog)
    assert search_catalog("fuel pump", {"relation": "analog"}).total == 1

    link.is_confirmed = False
    link.save(update_fields=["is_confirmed"])

    assert search_catalog("fuel pump", {"relation": "analog"}).total == 0


def test_relation_filter_composes_with_search_ranking_and_pagination(public_catalog):
    original = public_catalog.part("Starter OEM", article="ST-000")
    analog_ids = []
    for index in range(PAGE_SIZE + 5):
        analog = public_catalog.part(f"Starter analog {index:02d}", article=f"ST-{index + 1:03d}")
        public_catalog.analog(original, analog)
        analog_ids.append(analog.public_id)
    for index in range(10):
        public_catalog.part(f"Starter unrelated {index:02d}", article=f"SU-{index:03d}")

    first = search_catalog("starter", {"relation": "analog"})
    second = search_catalog("starter", {"relation": "analog", "page": "2"})

    assert first.total == PAGE_SIZE + 5
    assert first.pages == 2
    assert len(first.cards) == PAGE_SIZE and len(second.cards) == 5
    assert set(_ids(first)).isdisjoint(_ids(second))
    assert set(_ids(first)) | set(_ids(second)) == set(analog_ids)
    next_query = parse_qs(urlparse(first.url(page=2)).query)
    assert next_query == {"q": ["starter"], "relation": ["analog"], "page": ["2"]}


# --- Paging and URLs --------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["abc", "-1", "0", "", None, "9" * 40, "2.5", "1e3"])
def test_malformed_page_is_page_one(raw):
    assert parse_page(raw) == 1


def test_page_beyond_the_end_shows_the_last_page(public_catalog):
    for index in range(3):
        public_catalog.part(f"Grip heater {index}", article=f"GH-{index}")
    result = search_catalog("grip heater", {"page": "999"})
    assert result.page == 1 and len(result.cards) == 3


def test_malformed_page_parameter_is_not_a_server_error(public_client, public_catalog):
    public_catalog.part("Throttle cable", article="TC-1")
    for page in ("abc", "9" * 5000, "-3", "%00"):
        response = public_client.get("/search/", {"q": "throttle", "page": page})
        assert response.status_code == 200, page


def test_filter_urls_are_shareable_and_pagination_preserves_them(public_client, public_catalog):
    for index in range(PAGE_SIZE + 2):
        part = public_catalog.part(
            f"Seal kit {index:02d}", article=f"SK-{index:02d}", maker="ATHENA"
        )
        public_catalog.stock(part, "1")

    response = public_client.get(
        "/search/", {"q": "seal kit", "manufacturer": "ATHENA", "in_stock": "1"}
    )
    body = response.content.decode()

    assert response.status_code == 200
    assert "?q=seal+kit&amp;manufacturer=ATHENA&amp;in_stock=1&amp;page=2" in body
    # Each selected filter is a removable chip whose link keeps the others.
    assert "?q=seal+kit&amp;manufacturer=ATHENA" in body
    assert "?q=seal+kit&amp;in_stock=1" in body


def test_empty_results_are_distinguished_from_filtered_out_results(public_client, public_catalog):
    public_catalog.part("Exhaust gasket", article="EG-1")

    nothing = public_client.get("/search/", {"q": "zzzqqq"}).content.decode()
    filtered = public_client.get("/search/", {"q": "exhaust", "in_stock": "1"}).content.decode()

    assert "ничего не нашлось" in nothing
    assert "С этими фильтрами ничего нет" in filtered
    assert "Показать все результаты" in filtered


# --- Bounded, read-only work ------------------------------------------------------------


def _seed_family(public_catalog, size):
    original = public_catalog.part("Family original", article="FAM-000")
    for index in range(size):
        part = public_catalog.part(
            f"Family member {index:03d}",
            article=f"FAM-{index + 1:03d}",
            maker=f"MAKER {index % 4}",
            application="ГИДРОЦИКЛ" if index % 2 else "",
        )
        public_catalog.stock(part, "1")
        public_catalog.analog(original, part)
        public_catalog.image(part)
    from apps.catalog.models import PartTypeImage
    from apps.catalog.public_photos import publish_photo

    for image in PartTypeImage.objects.filter(part__name__startswith="Family member"):
        publish_photo(image, source="own", by=public_catalog.user)


@pytest.mark.parametrize("size", [1, 20, 50])
def test_search_page_query_count_does_not_grow_with_results(public_catalog, size, record_property):
    _seed_family(public_catalog, size)
    params = {"manufacturer": "MAKER 1", "relation": "analog", "in_stock": "1"}

    with capture() as queries:
        result = search_catalog("family member", params, page_size=50)

    assert result.total == len([i for i in range(size) if i % 4 == 1])
    assert_no_writes(queries)
    record_property(f"public_search_filters_queries_{size}", len(queries.captured_queries))
    # Search tiers (up to 9 on SQLite), visibility, applications (2),
    # relations (1), availability, then the hydrated window. Flat in N.
    assert len(queries.captured_queries) <= _QUERY_BUDGET[connection.vendor]


@pytest.mark.parametrize("size", [1, 20, 50])
def test_search_view_query_count_is_flat(public_client, public_catalog, size, record_property):
    _seed_family(public_catalog, size)

    with capture() as queries:
        response = public_client.get("/search/", {"q": "family member", "relation": "analog"})

    assert response.status_code == 200
    assert_no_writes(queries)
    record_property(f"public_search_view_queries_{size}", len(queries.captured_queries))
    assert len(queries.captured_queries) <= _QUERY_BUDGET[connection.vendor]
