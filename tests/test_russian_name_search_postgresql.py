"""Русский поиск на настоящей PostgreSQL 16: опечатки, индексы и локаль.

    DENSTOCK_TEST_DATABASE_URL=postgres://... pytest tests/test_russian_name_search_postgresql.py

Главное, что проверяется здесь и не проверяется на SQLite: русские тиры больше
не зависят от `UPPER()` базы. `UPPER` считает регистр по локали кластера и в
локали `C` кириллицу не сворачивает вовсе, поэтому свёртка ушла в Python, а
сравнение идёт по колонке `search_name_ru`. Тест доказывает это структурно - по
самому SQL, который уходит в базу, - а не только по результату на кластере с
удачной локалью.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.actions.models import PartCustomsInfo
from apps.catalog.search import cyrillic_fuzzy_available, search_part_ids, supports_trigram
from apps.core.search_text import fold_search_text
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


def _hit_for(query, part):
    return next((hit for hit in search_part_ids(query) if hit.part_id == part.pk), None)


# --- Независимость от локали -----------------------------------------------------------


def test_russian_tiers_never_ask_the_database_to_fold_case(cat):
    """Ни один запрос по русскому названию не должен звать UPPER по нему.

    Это и есть защита от кластера с локалью `C`: там UPPER кириллицу не
    трогает, и любой такой запрос молча перестаёт находить.
    """
    part = cat.part("LOCALE PROOF", article="LP-1")
    cat.russian(part, "Прокладка головки блока")

    for query in ("прокладка", "ПРОКЛАДКА", "головки блока", "головки прокладка", "проклатка"):
        with CaptureQueriesContext(connection) as captured:
            search_part_ids(query)
        for sql in (item["sql"] for item in captured.captured_queries):
            upper = sql.upper()
            if "CUSTOMS_NAME_RU" in upper:
                assert "UPPER(" not in upper.split("CUSTOMS_NAME_RU")[0][-40:], sql
            assert "UPPER(CI.CUSTOMS_NAME_RU" not in upper.replace(" ", ""), sql


def test_the_folded_column_holds_what_python_folded(cat):
    part = cat.part("FOLD STORAGE", article="FS-1")
    cat.russian(part, "ЩЁТКА Стеклоочистителя")

    stored = PartCustomsInfo.objects.get(part_type=part).search_name_ru

    assert stored == fold_search_text("ЩЁТКА Стеклоочистителя") == "щетка стеклоочистителя"


def test_this_cluster_reports_whether_cyrillic_typos_can_work(db):
    """Ответ зависит от локали кластера; ops_check показывает его оператору."""
    assert supports_trigram()
    assert isinstance(cyrillic_fuzzy_available(), bool)


# --- Опечатки ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("проклатка", "Прокладка головки блока"),
        ("прокладко", "Прокладка головки блока"),
        ("поршен", "Поршень тормоза"),
        ("подшибник", "Подшипник ступицы"),
    ],
)
def test_a_cyrillic_typo_finds_the_confirmed_name(cat, typed, stored):
    if not cyrillic_fuzzy_available():
        pytest.skip("локаль кластера не даёт триграмм для кириллицы")
    part = cat.part(f"TYPO {stored}", article=f"T-{abs(hash(stored)) % 9999}")
    cat.russian(part, stored)

    hit = _hit_for(typed, part)

    assert hit is not None, typed
    assert hit.match_type in ("name_prefix", "name_partial", "name_fuzzy")


def test_a_typo_still_never_reaches_an_unconfirmed_name(cat):
    if not cyrillic_fuzzy_available():
        pytest.skip("локаль кластера не даёт триграмм для кириллицы")
    part = cat.part("UNCONFIRMED TYPO", article="UT-1")
    cat.russian(part, "Прокладка головки блока", confirmed=False)

    assert _hit_for("проклатка", part) is None


def test_a_typo_in_a_yo_word_is_found_by_either_spelling(cat):
    if not cyrillic_fuzzy_available():
        pytest.skip("локаль кластера не даёт триграмм для кириллицы")
    part = cat.part("YO TYPO", article="YT-1")
    cat.russian(part, "Щётка стеклоочистителя")

    assert _hit_for("щетко", part) is not None
    assert _hit_for("щётко", part) is not None


# --- Индексы ----------------------------------------------------------------------------


def test_the_folded_trigram_index_is_partial_for_confirmed_names(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            ["actions_partcustomsinfo_search_ru_trgm"],
        )
        row = cursor.fetchone()

    assert row is not None
    assert "gin_trgm_ops" in row[0]
    assert "search_name_ru" in row[0]
    assert "WHERE customs_name_ru_confirmed" in row[0]


def test_the_old_upper_index_is_gone(db):
    """Выражение UPPER(customs_name_ru) больше не встречается ни в одном запросе."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE indexname = %s",
            ["actions_partcustomsinfo_ru_upper_trgm"],
        )
        assert cursor.fetchone() is None


@pytest.fixture
def indexed_catalog(cat):
    for index in range(400):
        part = cat.part(f"GASKET {index:05d}", article=f"{500000000 + index * 13}")
        if index % 4 == 0:
            cat.russian(part, f"Прокладка головки {index:05d}")
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE catalog_parttype")
        cursor.execute("ANALYZE actions_partcustomsinfo")
    return cat


def _plans_for(query):
    plans = []
    with CaptureQueriesContext(connection) as captured:
        search_part_ids(query)
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("SET LOCAL enable_indexscan = off")
        for item in captured.captured_queries:
            sql = item["sql"]
            if not sql.lstrip().upper().startswith("SELECT"):
                continue
            try:
                cursor.execute(f"EXPLAIN {sql}")
            except Exception:  # noqa: BLE001 - план не для каждого запроса нужен
                continue
            plans.append("\n".join(row[0] for row in cursor.fetchall()))
    return plans


@pytest.mark.parametrize("query", ["прокладка", "головки 00004"])
def test_a_russian_query_can_be_served_by_the_folded_index(indexed_catalog, query):
    plans = _plans_for(query)

    assert any("actions_partcustomsinfo_search_ru_trgm" in plan for plan in plans), (
        "\n\n".join(plans)[:1500]
    )


def test_russian_search_writes_nothing(cat):
    part = cat.part("READ ONLY", article="RO-1")
    cat.russian(part, "Прокладка головки блока")

    with CaptureQueriesContext(connection) as captured:
        search_part_ids("прокладка")
        search_part_ids("головки прокладка")
        search_part_ids("проклатка")

    assert_no_writes(captured)
