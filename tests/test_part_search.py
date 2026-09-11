"""Search 2.0 - the shared ranked part search (portable part of the contract).

Runs on SQLite and PostgreSQL. Typo tolerance and index-usage proofs need
pg_trgm and live in tests/test_part_search_postgresql.py.

The central guarantee under test: an exact identifier ALWAYS wins. Tiers decide
the order before any score does, so no fuzzy or partial name can outrank the
part whose article the operator typed.
"""
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.catalog.models import PartNumber, PartType
from apps.catalog.public_contracts import build_public_part_facts
from apps.catalog.search import (
    MATCH_TYPE_RANKS,
    MAX_PAGE_SIZE,
    MAX_QUERY_LENGTH,
    MIN_PARTIAL_LENGTH,
    MIN_PREFIX_LENGTH,
    RESULT_CAP,
    PartSearchHit,
    clean_query,
    search_part_ids,
    search_parts,
)
from tests.search_support import Catalog, assert_no_writes


@pytest.fixture
def cat(db):
    return Catalog()


def _types(hits):
    return [hit.match_type for hit in hits]


def _ids(hits):
    return [hit.part_id for hit in hits]


def _first(query):
    hits = search_part_ids(query)
    return hits[0] if hits else None


# --- Article ----------------------------------------------------------------------------


def test_exact_canonical_article(cat):
    part = cat.part("Belt", article="420-892-388")
    hit = _first("420-892-388")
    assert hit.part_id == part.pk
    assert hit.match_type == "exact_article"


def test_normalized_exact_article(cat):
    part = cat.part("Belt", article="420-892-388")
    hit = _first("420892388")
    assert hit.part_id == part.pk
    assert hit.match_type == "normalized_exact_article"


@pytest.mark.parametrize(
    "typed",
    ["420892388", "420-892-388", "420 892 388", "420_892_388", "420.892.388", "420/892/388"],
)
def test_every_separator_resolves_to_the_same_part(cat, typed):
    """Reuses the canonical normalize_number: spaces, hyphens, _ . / all fold."""
    part = cat.part("Belt", article="420-892-388")
    assert _first(typed).part_id == part.pk


def test_article_matching_is_case_insensitive(cat):
    part = cat.part("Gasket", article="AB-1234-CD")
    assert _first("ab-1234-cd").part_id == part.pk
    assert _first("ab-1234-cd").match_type == "exact_article"


def test_article_prefix(cat):
    part = cat.part("Pump", article="715900111")
    hit = _first("7159")
    assert hit.part_id == part.pk and hit.match_type == "article_prefix"


def test_article_substring(cat):
    part = cat.part("Pump", article="715900111")
    hit = _first("9001")
    assert hit.part_id == part.pk and hit.match_type == "article_partial"


def test_exact_article_outranks_a_partial_article(cat):
    exact = cat.part("Exact", article="123456")
    cat.part("Longer", article="1234567890")
    hits = search_part_ids("123456")
    assert hits[0].part_id == exact.pk
    assert hits[0].match_type == "exact_article"
    assert hits[1].match_type == "article_prefix"


def test_exact_article_outranks_a_name_containing_the_same_digits(cat):
    """The decoy is a part whose NAME contains the typed article."""
    exact = cat.part("Special exact part", article="420-892-388")
    decoy = cat.part("BEARING 420892388 DECOY", article="999000111")
    hits = search_part_ids("420892388")
    assert hits[0].part_id == exact.pk
    assert decoy.pk in _ids(hits)
    assert _ids(hits).index(exact.pk) < _ids(hits).index(decoy.pk)


def test_auxiliary_and_internal_numbers_are_not_public_identity(cat):
    """Only OEM/ARTICLE are canonical identity, as everywhere else in DenisStock."""
    cat.part("Hidden", article="555000777", kind=PartNumber.Kind.ANALOG)
    cat.part("Internal", article="666000888", kind=PartNumber.Kind.INTERNAL_REF)
    assert search_part_ids("555000777") == []
    assert search_part_ids("666000888") == []


def test_duplicate_article_returns_every_candidate_deterministically(cat):
    """Duplicate articles exist in the domain; none is silently picked."""
    first = cat.part("Duplicate A", article="777-111")
    second = cat.part("Duplicate B", article="777111")
    hits = search_part_ids("777111")
    assert set(_ids(hits)) == {first.pk, second.pk}
    assert search_part_ids("777111") == hits, "same query, same order"
    # the one typed exactly as stored wins; the other is a normalized match
    by_part = {hit.part_id: hit.match_type for hit in hits}
    assert by_part[second.pk] == "exact_article"
    assert by_part[first.pk] == "normalized_exact_article"


def test_too_short_query_skips_broad_scans(cat):
    cat.part("Thing", article="4200000")
    with CaptureQueriesContext(connection) as cap:
        hits = search_part_ids("42")
    assert hits == []
    sql = " ".join(q["sql"] for q in cap.captured_queries)
    # Equality is allowed (on SQLite Django spells iexact as a wildcard-free
    # LIKE); a prefix or substring pattern is not.
    assert "%42" not in sql and "42%" not in sql, "short query ran a broad scan"


def test_short_exact_identifier_still_resolves(cat):
    """Exact identifiers keep working below the floors: one indexed lookup."""
    part = cat.part("Tiny", article="42")
    hit = _first("42")
    assert hit.part_id == part.pk and hit.match_type == "exact_article"


# --- English ----------------------------------------------------------------------------


def test_exact_english_name(cat):
    part = cat.part("Drive Belt")
    hit = _first("Drive Belt")
    assert hit.part_id == part.pk and hit.match_type == "exact_name"


def test_english_is_case_insensitive(cat):
    part = cat.part("Drive Belt")
    assert _first("dRiVe bElT").part_id == part.pk
    assert _first("dRiVe bElT").match_type == "exact_name"


def test_english_prefix(cat):
    part = cat.part("Sprocket Assembly")
    hit = _first("Sprock")
    assert hit.part_id == part.pk and hit.match_type == "name_prefix"


def test_english_substring(cat):
    part = cat.part("Rear Sprocket Assembly")
    hit = _first("ocket")
    assert hit.part_id == part.pk and hit.match_type == "name_partial"


# --- Russian ----------------------------------------------------------------------------


def test_exact_confirmed_russian_name(cat):
    part = cat.part("Piston")
    cat.russian(part, "ПОРШЕНЬ")
    hit = _first("ПОРШЕНЬ")
    assert hit.part_id == part.pk and hit.match_type == "exact_name"


# Case-insensitive Cyrillic is PostgreSQL behaviour: SQLite's UPPER and LIKE
# fold ASCII only. The portable tests below use the stored case; mixed-case
# Russian is covered in tests/test_part_search_postgresql.py.


def test_russian_prefix(cat):
    part = cat.part("Gasket")
    cat.russian(part, "ПРОКЛАДКА ГОЛОВКИ")
    hit = _first("ПРОКЛ")
    assert hit.part_id == part.pk and hit.match_type == "name_prefix"


def test_russian_substring(cat):
    part = cat.part("Gasket")
    cat.russian(part, "ПРОКЛАДКА ГОЛОВКИ")
    hit = _first("ЛОВКИ")
    assert hit.part_id == part.pk and hit.match_type == "name_partial"


def test_unconfirmed_russian_name_never_matches(cat):
    """A generated or unreviewed translation is not trusted public content."""
    part = cat.part("Secret")
    cat.russian(part, "СЕКРЕТНОЕ НАЗВАНИЕ", confirmed=False)
    for query in ("СЕКРЕТНОЕ НАЗВАНИЕ", "СЕКРЕТНОЕ", "КРЕТНО"):
        assert part.pk not in _ids(search_part_ids(query)), query


def test_confirmed_but_blank_russian_name_is_safe(cat):
    """A whitespace RU name matches nothing and never widens a search.

    The English name shares no letters with the queries, so the only way this
    part could appear is through its (blank) Russian name.
    """
    part = cat.part("QXZ-7")
    cat.russian(part, "   ")
    assert search_part_ids("   ") == []
    for query in ("ПОРШЕНЬ", "поршень", "ремень", "bearing"):
        assert part.pk not in _ids(search_part_ids(query)), query


def test_same_part_matching_english_and_russian_appears_once(cat):
    part = cat.part("Piston")
    cat.russian(part, "PISTON")
    hits = search_part_ids("Piston")
    assert _ids(hits).count(part.pk) == 1


# --- Ranking ----------------------------------------------------------------------------


def test_ranking_contract_is_the_documented_tier_order():
    assert [t for t, _ in sorted(MATCH_TYPE_RANKS.items(), key=lambda kv: kv[1])] == [
        "exact_article", "normalized_exact_article", "article_prefix", "article_partial",
        "exact_name", "name_prefix", "name_partial", "name_fuzzy",
    ]


def test_exact_name_outranks_a_prefix_name(cat):
    exact = cat.part("Belt")
    cat.part("Belt Drive Long")
    hits = search_part_ids("Belt")
    assert hits[0].part_id == exact.pk and hits[0].match_type == "exact_name"


def test_hits_are_ordered_by_tier_then_score_then_part_id(cat):
    parts = [cat.part(f"Washer {i}") for i in range(6)]
    hits = search_part_ids("Washer")
    ranks = [hit.rank for hit in hits]
    assert ranks == sorted(ranks)
    same_tier = [hit.part_id for hit in hits if hit.match_type == "name_prefix"]
    assert same_tier == sorted(same_tier), "ties inside a tier break by part id"
    assert {p.pk for p in parts} <= set(_ids(hits))


def test_repeated_search_is_identical(cat):
    for i in range(8):
        cat.part(f"Spring {i}", article=f"SPR-{i:03d}")
    assert search_part_ids("Spring") == search_part_ids("Spring")


def test_a_part_keeps_only_its_best_tier(cat):
    part = cat.part("Bolt", article="BOLT-9000")
    hits = search_part_ids("BOLT-9000")
    assert _ids(hits).count(part.pk) == 1
    assert hits[0].match_type == "exact_article"


def test_hit_is_an_identity_not_a_presentation_object(cat):
    cat.part("Identity", article="ID-1")
    hit = _first("ID-1")
    assert isinstance(hit, PartSearchHit)
    assert set(hit.__slots__) == {"part_id", "rank", "score", "match_type"}


# --- Pagination -------------------------------------------------------------------------


def _catalog_of(cat, count, stem="Filter"):
    return [cat.part(f"{stem} {i:03d}") for i in range(count)]


def test_pages_are_stable_and_never_repeat_a_part(cat):
    _catalog_of(cat, 45)
    first = search_parts("Filter", page=1, page_size=20)
    second = search_parts("Filter", page=2, page_size=20)
    third = search_parts("Filter", page=3, page_size=20)
    seen = _ids(first.hits) + _ids(second.hits) + _ids(third.hits)
    assert len(seen) == len(set(seen)) == 45
    assert first.has_more and second.has_more and not third.has_more
    assert search_parts("Filter", page=2, page_size=20) == second


def test_page_size_is_capped(cat):
    _catalog_of(cat, 60)
    page = search_parts("Filter", page=1, page_size=10_000)
    assert page.page_size == MAX_PAGE_SIZE
    assert len(page.hits) == MAX_PAGE_SIZE


def test_invalid_page_input_is_clamped(cat):
    _catalog_of(cat, 3)
    assert search_parts("Filter", page=0, page_size=0).page == 1
    assert search_parts("Filter", page=-5, page_size=-1).page_size >= 1


def test_page_beyond_the_end_is_empty_not_an_error(cat):
    _catalog_of(cat, 3)
    page = search_parts("Filter", page=99, page_size=20)
    assert page.hits == [] and page.total == 3 and not page.has_more


def test_result_set_is_capped(cat):
    """No query can make the engine hold more than RESULT_CAP identities."""
    _catalog_of(cat, RESULT_CAP + 25, stem="Nut")
    hits = search_part_ids("Nut")
    assert len(hits) == RESULT_CAP
    assert search_parts("Nut").truncated is True


# --- Input hygiene ----------------------------------------------------------------------


def test_query_is_trimmed_collapsed_and_length_capped():
    assert clean_query("  drive    belt  ") == "drive belt"
    assert len(clean_query("x" * 5000)) == MAX_QUERY_LENGTH
    assert clean_query(None) == ""


@pytest.mark.parametrize("empty", ["", "   ", None, "\t\n"])
def test_empty_query_returns_nothing(cat, empty):
    cat.part("Anything", article="1")
    assert search_part_ids(empty) == []


def test_documented_floors_are_ordered():
    assert 1 <= MIN_PREFIX_LENGTH <= MIN_PARTIAL_LENGTH


# --- Visibility -------------------------------------------------------------------------


def test_zero_stock_part_is_searchable(cat):
    """Search and availability are separate: out of stock is still findable."""
    part = cat.part("Out Of Stock Belt", article="OOS-1")
    assert _first("OOS-1").part_id == part.pk
    facts = build_public_part_facts([part.pk])[0]
    assert facts.available_quantity == Decimal("0")


def test_inactive_parts_follow_current_internal_behaviour(cat):
    """Internal lookup does not filter is_active today; search matches that.

    Final publication filtering (``is_public``) belongs to a later Public
    Catalog stage and plugs in as a filter on the returned identities.
    """
    part = cat.part("Archived Belt", article="ARCH-1", active=False)
    assert _first("ARCH-1").part_id == part.pk


# --- Stage 1 integration ----------------------------------------------------------------


def test_hits_hydrate_through_stage1_public_facts_in_rank_order(cat):
    exact = cat.part("Target", article="TGT-100")
    cat.part("Target Longer", article="TGT-1000")
    hits = search_part_ids("TGT-100")
    facts = build_public_part_facts([hit.part_id for hit in hits])
    # Facts carry the opaque public identity, never the warehouse primary key.
    public_ids = dict(PartType.objects.values_list("pk", "public_id"))
    assert [f.public_id for f in facts] == [public_ids[hit.part_id] for hit in hits]
    assert facts[0].public_id == exact.public_id
    assert facts[0].article == "TGT-100"


# --- Pure read & bounded queries --------------------------------------------------------


def test_search_writes_nothing(cat):
    part = cat.part("Belt", article="420-892-388")
    cat.russian(part, "РЕМЕНЬ")
    with CaptureQueriesContext(connection) as cap:
        for query in ("420-892-388", "420892388", "Belt", "ремень", "rem", "zzzzzz"):
            search_part_ids(query)
            search_parts(query, page=1)
    assert_no_writes(cap)
    assert cap.captured_queries


# Worst case: nine tier queries plus, for a name query on PostgreSQL inside an
# outer transaction, SAVEPOINT / read setting / set setting / fuzzy / restore /
# RELEASE. Production requests run in autocommit, where the same search is 13.
MAX_SEARCH_QUERIES = 15


def test_query_count_does_not_scale_with_matches(cat):
    """The N+1 property: 1, 20 and 50 matches cost the same number of queries."""
    counts = []
    for batch, size in (("Bushing", 1), ("Spacer", 20), ("Grommet", 50)):
        _catalog_of(cat, size, stem=batch)
        with CaptureQueriesContext(connection) as cap:
            hits = search_part_ids(batch)
        assert len(hits) == size
        counts.append(len(cap.captured_queries))
    assert counts[0] == counts[1] == counts[2], counts
    assert counts[0] <= MAX_SEARCH_QUERIES, counts


def test_search_selects_only_primary_keys(cat):
    """No catalog row is materialised in Python: every tier returns ids only."""
    cat.part("Belt", article="420-892-388")
    with CaptureQueriesContext(connection) as cap:
        search_part_ids("Belt")
    for query in cap.captured_queries:
        sql = query["sql"].upper()
        if not sql.lstrip().startswith("SELECT") or "SET_CONFIG" in sql:
            continue
        assert '"DESCRIPTION"' not in sql and '"RECOMMENDED_PRICE"' not in sql, sql[:90]


def test_search_plus_hydration_is_bounded_end_to_end(cat):
    for i in range(50):
        cat.part(f"Clutch {i:02d}", article=f"CL-{i:02d}")
    with CaptureQueriesContext(connection) as search_cap:
        page = search_parts("Clutch", page=1, page_size=50)
    with CaptureQueriesContext(connection) as hydrate_cap:
        facts = build_public_part_facts([hit.part_id for hit in page.hits])
    assert len(facts) == 50
    assert len(hydrate_cap.captured_queries) <= 8
    assert len(search_cap.captured_queries) <= MAX_SEARCH_QUERIES
