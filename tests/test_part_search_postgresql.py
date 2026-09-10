"""Search 2.0 on PostgreSQL 16: typo tolerance and index usage.

Needs pg_trgm, so it runs only against PostgreSQL:

    DENSTOCK_TEST_DATABASE_URL=postgres://... pytest tests/test_part_search_postgresql.py

Index usage is proven from EXPLAIN output, not from the fact that an index
exists: the planner is free to ignore an index, so only the plan is evidence.
"""
import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from apps.catalog.search import (
    WORD_SIMILARITY_THRESHOLD,
    search_part_ids,
    supports_trigram,
)
from tests.search_support import Catalog, assert_no_writes

pytestmark = pytest.mark.postgresql

if connection.vendor != "postgresql":
    pytest.skip(
        "Run against PostgreSQL 16 with DENSTOCK_TEST_DATABASE_URL",
        allow_module_level=True,
    )


@pytest.fixture
def cat(db):
    return Catalog()


def _ids(hits):
    return [hit.part_id for hit in hits]


def _hit_for(query, part):
    return next((hit for hit in search_part_ids(query) if hit.part_id == part.pk), None)


# --- Extension and indexes --------------------------------------------------------------


def test_pg_trgm_is_installed(db):
    assert supports_trigram()
    with connection.cursor() as cursor:
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'")
        assert cursor.fetchone() is not None


@pytest.mark.parametrize(
    "index",
    [
        "catalog_partnumber_normalized_trgm",
        "catalog_parttype_name_upper_trgm",
        "actions_partcustomsinfo_ru_upper_trgm",
    ],
)
def test_trigram_index_exists(db, index):
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", [index])
        row = cursor.fetchone()
    assert row is not None, index
    assert "gin_trgm_ops" in row[0]


def test_russian_trigram_index_is_partial_for_confirmed_names(db):
    """The index population must not include unreviewed public text."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            ["actions_partcustomsinfo_ru_upper_trgm"],
        )
        indexdef = cursor.fetchone()[0]
    assert "WHERE customs_name_ru_confirmed" in indexdef


# --- Typo tolerance ---------------------------------------------------------------------


def test_english_typo_finds_the_word_inside_a_long_name(cat):
    """Whole-string similarity would miss this (0.227); word similarity finds it."""
    part = cat.part("BEARING DRIVE 12345")
    hit = _hit_for("bearng", part)
    assert hit is not None and hit.match_type == "name_fuzzy"
    assert hit.score >= WORD_SIMILARITY_THRESHOLD


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("поршен", "ПОРШЕНЬ ТОРМОЗА"), ("проклатка", "ПРОКЛАДКА ГОЛОВКИ")],
)
def test_russian_typo_finds_the_confirmed_name(cat, typed, stored):
    part = cat.part("Russian typo target")
    cat.russian(part, stored)
    hit = _hit_for(typed, part)
    assert hit is not None, typed
    assert hit.match_type in ("name_prefix", "name_fuzzy")


def test_typo_does_not_match_unrelated_words(cat):
    """The measured noise pairs score 0.20-0.29, below the 0.5 cut-off."""
    belt = cat.part("BELT DRIVE 00001")
    spring = cat.part("Spring typo noise")
    cat.russian(spring, "ПРУЖИНА ТОРМОЗА")
    assert _hit_for("bearng", belt) is None
    assert _hit_for("проклатка", spring) is None


def test_unconfirmed_russian_name_is_excluded_from_fuzzy(cat):
    part = cat.part("Unconfirmed fuzzy")
    cat.russian(part, "ПРОКЛАДКА ГОЛОВКИ", confirmed=False)
    assert _hit_for("проклатка", part) is None
    assert _hit_for("прокладка", part) is None


def test_fuzzy_sql_constrains_russian_candidates_before_similarity(cat):
    """Confirmation is a SQL predicate, not an after-the-fact Python filter."""
    confirmed = cat.part("Confirmed gasket")
    cat.russian(confirmed, "ПРОКЛАДКА ГОЛОВКИ")
    unconfirmed = cat.part("Unconfirmed gasket")
    cat.russian(unconfirmed, "ПРОКЛАДКА ГОЛОВКИ", confirmed=False)
    with CaptureQueriesContext(connection) as cap:
        hits = search_part_ids("проклатка")
    fuzzy_sql = next(
        query["sql"]
        for query in cap.captured_queries
        if "WORD_SIMILARITY(" in query["sql"].upper()
    )
    assert "CUSTOMS_NAME_RU_CONFIRMED" in fuzzy_sql.upper()
    assert confirmed.pk in _ids(hits)
    assert unconfirmed.pk not in _ids(hits)


def test_fuzzy_name_never_outranks_an_exact_article(cat):
    exact = cat.part("Totally unrelated name", article="BEARNG-01")
    cat.part("BEARING DRIVE 12345")
    hits = search_part_ids("BEARNG-01")
    assert hits[0].part_id == exact.pk
    assert hits[0].match_type == "exact_article"


def test_exact_name_outranks_fuzzy(cat):
    exact = cat.part("BEARNG")
    fuzzy = cat.part("BEARING DRIVE 12345")
    hits = search_part_ids("bearng")
    assert hits[0].part_id == exact.pk and hits[0].match_type == "exact_name"
    assert _ids(hits).index(exact.pk) < _ids(hits).index(fuzzy.pk)


def test_fuzzy_is_skipped_for_a_query_without_letters(cat):
    """Typo tolerance is for words; a bare number is an identifier."""
    cat.part("BEARING 420892388")
    with CaptureQueriesContext(connection) as cap:
        search_part_ids("420892388")
    assert not any("WORD_SIMILARITY" in q["sql"].upper() for q in cap.captured_queries)


# --- Russian case folding ---------------------------------------------------------------


def test_russian_is_case_insensitive_on_postgresql(cat):
    part = cat.part("Case folding")
    cat.russian(part, "ПОРШЕНЬ ДВИГАТЕЛЯ")
    hit = _hit_for("поршень двигателя", part)
    assert hit is not None and hit.match_type == "exact_name"
    assert _hit_for("поршень", part).match_type == "name_prefix"
    assert _hit_for("двигат", part).match_type == "name_partial"


# --- Setting hygiene and pure read ------------------------------------------------------


def test_threshold_setting_does_not_leak_into_the_connection(cat):
    cat.part("BEARING DRIVE 12345")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('pg_trgm.word_similarity_threshold')")
        before = cursor.fetchone()[0]
    search_part_ids("bearng")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('pg_trgm.word_similarity_threshold')")
        after = cursor.fetchone()[0]
    assert after == before


def _threshold():
    # ``missing_ok``: pg_trgm registers the setting only once its library is
    # loaded into the backend, so a fresh connection may not know it yet.
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('pg_trgm.word_similarity_threshold', true)")
        return cursor.fetchone()[0]


def _load_trgm():
    with connection.cursor() as cursor:
        cursor.execute("SELECT show_trgm('x')")


def test_threshold_is_restored_after_atomic_and_nested_atomic(cat):
    cat.part("BEARING DRIVE 12345")
    _load_trgm()
    baseline = _threshold()
    with transaction.atomic():
        assert search_part_ids("bearng")
        assert _threshold() == baseline
        with transaction.atomic():
            assert search_part_ids("bearng")
            assert _threshold() == baseline
        assert _threshold() == baseline
    assert _threshold() == baseline


def test_threshold_is_not_left_behind_by_a_failing_fuzzy_query(cat, monkeypatch):
    """An error inside the fuzzy block rolls its savepoint back, setting included."""
    import apps.catalog.search as search

    cat.part("BEARING DRIVE 12345")
    _load_trgm()
    baseline = _threshold()
    monkeypatch.setattr(
        search, "_FUZZY_SQL", "SELECT this_function_does_not_exist(%s, %s, %s, %s, %s)"
    )
    with transaction.atomic():
        with pytest.raises(Exception):  # noqa: B017 - any database error will do
            search.search_part_ids("bearng")
        assert _threshold() == baseline
    assert _threshold() == baseline


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_fresh_backend_fuzzy_search_in_autocommit_and_inside_atomic():
    """A brand-new connection has not loaded pg_trgm; search must still work.

    Inside an outer transaction the fuzzy block first reads the current
    threshold so it can put it back. That read needs the setting to exist, and
    on a fresh backend it exists only because the name tiers have already run
    and loaded pg_trgm while planning against its GIN index. The name partial
    tier has the same length floor as the fuzzy tier, so it always runs first;
    this test pins that ordering, in autocommit and inside ``atomic``.
    """
    catalog = Catalog()
    catalog.part("BEARING DRIVE 12345")
    connection.close()
    assert _threshold() is None  # really a fresh backend
    assert search_part_ids("bearng")
    _load_trgm()
    baseline = _threshold()
    assert search_part_ids("bearng") and _threshold() == baseline

    connection.close()
    with transaction.atomic():
        assert search_part_ids("bearng")
    _load_trgm()
    assert _threshold() == baseline


def test_fuzzy_search_writes_nothing(cat):
    part = cat.part("BEARING DRIVE 12345")
    cat.russian(part, "ПОДШИПНИК")
    with CaptureQueriesContext(connection) as cap:
        search_part_ids("bearng")
        search_part_ids("подшипнек")
    assert_no_writes(cap)


# --- Index usage proven by EXPLAIN ------------------------------------------------------


def _plans_for(query):
    """EXPLAIN every statement a search issues; return the plan texts."""
    with CaptureQueriesContext(connection) as cap:
        search_part_ids(query)
    plans = []
    for captured in cap.captured_queries:
        sql = captured["sql"].strip()
        if not sql.upper().startswith("SELECT") or "SET_CONFIG" in sql.upper():
            continue
        with connection.cursor() as cursor:
            # Forbid both sequential scans AND plain index scans. What remains
            # is bitmap scans, and a bitmap can only be built from an index
            # that serves the predicate itself. Without this, the planner may
            # satisfy "ORDER BY part_id LIMIT n" by walking the part_id index
            # and filtering - which would pass even if the trigram index were
            # missing, and prove nothing.
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_indexscan = off")
            cursor.execute("EXPLAIN " + sql)
            plans.append("\n".join(row[0] for row in cursor.fetchall()))
    return plans


@pytest.fixture
def indexed_catalog(cat):
    """Enough rows that the planner has a real choice to make."""
    for index in range(400):
        part = cat.part(f"BEARING DRIVE {index:05d}", article=f"{300000000 + index * 13}")
        if index % 4 == 0:
            cat.russian(part, "ПРОКЛАДКА ГОЛОВКИ")
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE catalog_parttype")
        cursor.execute("ANALYZE catalog_partnumber")
        cursor.execute("ANALYZE actions_partcustomsinfo")
    return cat


@pytest.mark.parametrize(
    ("query", "index"),
    [
        ("30000", "catalog_partnumber_normalized"),
        ("0001300", "catalog_partnumber_normalized_trgm"),
        ("drive", "catalog_parttype_name_upper_trgm"),
        ("bearng", "catalog_parttype_name_upper_trgm"),
        ("ПРОКЛАДКА", "actions_partcustomsinfo_ru_upper_trgm"),
    ],
)
def test_search_can_be_served_by_the_intended_index(indexed_catalog, query, index):
    """Only a predicate-serving index can appear once scans are restricted.

    With sequential and plain index scans disabled, the named index shows up
    in the plan only if it can answer the WHERE clause Django actually emits.
    That proves the expression in the migration matches the expression in the
    query - which an index merely existing would not.
    """
    plans = _plans_for(query)
    assert any(index in plan for plan in plans), "\n\n".join(plans)[:1500]
