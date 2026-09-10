"""Shared ranked part search for the future Public Catalog and internal reuse.

The engine answers one question only: which parts match this text, and in what
order. It returns part identities with an explicit match reason, never a
presentation object. Consumers hydrate separately - the Public Catalog through
``apps.catalog.public_contracts.build_public_part_facts``, internal screens
through their own DTOs. Nothing here exposes locations, lots, serials, costs,
suppliers, receipts, customers or staff.

Ranking is tiered. Every tier is its own indexed, LIMITed SQL query and the
tiers are concatenated in priority order, so an exact identifier always
outranks a fuzzy name no matter what the similarity score is: the tier decides
first, the score only orders inside a tier. The catalog is never loaded into
Python - each tier fetches at most ``RESULT_CAP`` primary keys, and the number
of queries is fixed, independent of how many parts match.

Typo tolerance is a PostgreSQL capability (pg_trgm). On SQLite, which the unit
test suite uses, the fuzzy tier is skipped and every other tier behaves the
same; fuzzy behaviour is covered by PostgreSQL-marked tests.

See docs/design/public-catalog-search.md for the full contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from django.db import connection, transaction

from apps.actions.models import PartCustomsInfo
from apps.inventory.presentation import EXACT_NUMBER_KINDS

from .models import PartNumber, PartType, normalize_number

MatchType = Literal[
    "exact_article",
    "normalized_exact_article",
    "article_prefix",
    "article_partial",
    "exact_name",
    "name_prefix",
    "name_partial",
    "name_fuzzy",
]

# Tier order IS the ranking contract. Lower rank wins, always.
MATCH_TYPE_RANKS: dict[str, int] = {
    "exact_article": 1,
    "normalized_exact_article": 2,
    "article_prefix": 3,
    "article_partial": 4,
    "exact_name": 5,
    "name_prefix": 6,
    "name_partial": 7,
    "name_fuzzy": 8,
}

# --- Thresholds -------------------------------------------------------------
# The public catalog will face the open internet, so every broad scan needs an
# explicit floor. Below the floors only the cheap indexed equality tiers run.
MAX_QUERY_LENGTH = 64
MIN_PREFIX_LENGTH = 3
MIN_PARTIAL_LENGTH = 4
MIN_FUZZY_LENGTH = 4
# Typo tolerance uses pg_trgm ``word_similarity``: how well the query matches
# the best WORD of a name, not the whole string. Whole-string ``similarity``
# dilutes a one-word typo inside a long catalog name ("bearng" vs "BEARING DRIVE
# 12345" scores 0.227 and is missed). Measured on PostgreSQL 16:
#   targets  bearng->BEARING 0.571, поршен->ПОРШЕНЬ 0.857, проклатка->ПРОКЛАДКА 0.600
#   noise    bearng->BELT 0.286, проклатка->ПРУЖИНА 0.200, поршен->ПОДШИПНИК 0.286
# 0.5 sits in that gap with margin on both sides. pg_trgm's own default is 0.6,
# which would miss "bearng", so the value is applied transaction-locally.
WORD_SIMILARITY_THRESHOLD = 0.5
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
RESULT_CAP = 300


@dataclass(frozen=True, slots=True)
class PartSearchHit:
    """One ranked part identity. Deliberately not a presentation object."""

    part_id: int
    rank: int
    score: float
    match_type: MatchType


@dataclass(frozen=True, slots=True)
class PartSearchPage:
    """A bounded, deterministic slice of the ranked result list."""

    hits: list[PartSearchHit]
    page: int
    page_size: int
    total: int
    has_more: bool
    truncated: bool


def supports_trigram() -> bool:
    """Typo tolerance needs PostgreSQL; every other tier works on any backend."""
    return connection.vendor == "postgresql"


def _has_letter(text: str) -> bool:
    return any(char.isalpha() for char in text)


def clean_query(raw: str | None) -> str:
    """Collapse whitespace and cap length before any database work happens."""
    return " ".join((raw or "").split())[:MAX_QUERY_LENGTH]


# --- Identifier tiers -------------------------------------------------------


def _identity_numbers():
    """Only the canonical identity kinds, never auxiliary or internal numbers."""
    return PartNumber.objects.filter(kind__in=EXACT_NUMBER_KINDS)


def _exact_article_split(query: str, normalized: str, limit: int):
    """Exact-as-typed and separator-insensitive matches from ONE indexed lookup.

    ``normalized_value`` is indexed; the raw ``value`` is not. So both tiers
    come from a single equality on the normalized column, and the tiny result
    set is split in Python by whether the operator typed the stored value
    itself or the same number with different separators.
    """
    if not normalized:
        return [], []
    rows = list(
        _identity_numbers()
        .filter(normalized_value=normalized)
        .order_by("part_id", "pk")
        .values_list("part_id", "value")[:limit]
    )
    typed = query.upper()
    exact = [part_id for part_id, value in rows if value.strip().upper() == typed]
    normalized_only = [part_id for part_id, _value in rows]
    return exact, normalized_only


def _article_ids(normalized: str, lookup: str, limit: int) -> list[int]:
    return list(
        _identity_numbers()
        .filter(**{f"normalized_value__{lookup}": normalized})
        .order_by("part_id")
        .values_list("part_id", flat=True)
        .distinct()[:limit]
    )


# --- Name tiers -------------------------------------------------------------


def _name_ids(query: str, lookup: str, limit: int) -> list[int]:
    """English catalog name OR confirmed Russian name, merged by primary key.

    Two separate queries on purpose: an OR across the join would stop
    PostgreSQL from using the index on either side.
    """
    english = PartType.objects.filter(**{f"name__{lookup}": query}).values_list(
        "pk", flat=True
    )[:limit]
    russian = PartCustomsInfo.objects.filter(
        customs_name_ru_confirmed=True, **{f"customs_name_ru__{lookup}": query}
    ).values_list("part_type_id", flat=True)[:limit]
    return sorted(set(english) | set(russian))[:limit]


_FUZZY_SQL = """
SELECT part_id, MAX(score) AS score FROM (
    SELECT pt.id AS part_id,
           word_similarity(UPPER(%s), UPPER(pt.name::text)) AS score
    FROM catalog_parttype pt
    WHERE UPPER(%s) <%% UPPER(pt.name::text)
  UNION ALL
    SELECT ci.part_type_id AS part_id,
           word_similarity(UPPER(%s), UPPER(ci.customs_name_ru::text)) AS score
    FROM actions_partcustomsinfo ci
    WHERE ci.customs_name_ru_confirmed
      AND UPPER(%s) <%% UPPER(ci.customs_name_ru::text)
) candidates
GROUP BY part_id
ORDER BY MAX(score) DESC, part_id
LIMIT %s
"""


def _fuzzy_rows(query: str, limit: int) -> list[tuple[int, float]]:
    """Typo-tolerant name matches through the pg_trgm ``<%`` operator.

    ``<%`` is what lets the GIN trigram index answer the query. Computing
    ``word_similarity()`` in a WHERE clause instead would score every catalog
    row. Each branch of the UNION uses its own expression index; the Russian
    branch reads confirmed names only.

    The operator's cut-off is the ``pg_trgm.word_similarity_threshold``
    setting, applied with ``is_local => true`` so it ends with the transaction.
    Normally this block IS the transaction and the setting reverts on commit by
    itself. Only when called inside an outer transaction would it outlive the
    search, so in that case the previous value is restored explicitly. Either
    way this is a session setting, never a data write.
    """
    if not supports_trigram():
        return []
    nested = connection.in_atomic_block
    with transaction.atomic(), connection.cursor() as cursor:
        previous = None
        if nested:
            cursor.execute("SELECT current_setting('pg_trgm.word_similarity_threshold')")
            previous = cursor.fetchone()[0]
        cursor.execute(
            "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
            [str(WORD_SIMILARITY_THRESHOLD)],
        )
        cursor.execute(_FUZZY_SQL, [query, query, query, query, limit])
        rows = [(int(part_id), float(score)) for part_id, score in cursor.fetchall()]
        if nested:
            cursor.execute(
                "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)", [previous]
            )
    return rows


# --- Public entry points ----------------------------------------------------


def search_part_ids(raw_query: str | None, *, limit: int = RESULT_CAP) -> list[PartSearchHit]:
    """Ranked part identities for a query, capped and deterministic.

    Tiers run in priority order and a part keeps only its best tier, so no part
    appears twice. Inside a tier the order is score then primary key, which
    keeps ties stable across calls and across pages.
    """
    query = clean_query(raw_query)
    if not query:
        return []
    limit = max(1, min(int(limit), RESULT_CAP))
    normalized = normalize_number(query)

    hits: list[PartSearchHit] = []
    seen: set[int] = set()

    def full() -> bool:
        return len(hits) >= limit

    def take(part_ids, match_type: str, scores: dict[int, float] | None = None) -> None:
        rank = MATCH_TYPE_RANKS[match_type]
        for part_id in part_ids:
            if len(hits) >= limit:
                return
            if part_id in seen:
                continue
            seen.add(part_id)
            hits.append(
                PartSearchHit(
                    part_id=part_id,
                    rank=rank,
                    score=scores[part_id] if scores else 1.0,
                    match_type=match_type,
                )
            )

    # 1-2. Identifier equality has no length floor: one indexed lookup stays
    # cheap even for a two-character article, so short exact queries work.
    exact, normalized_only = _exact_article_split(query, normalized, limit)
    take(exact, "exact_article")
    take(normalized_only, "normalized_exact_article")

    # Once the cap is full, no lower tier can place a part: anything it found
    # would rank below the parts already held and be cut by the cap. Skipping
    # those tiers gives the identical result with fewer queries.

    # 3-4. Prefix and substring are the broad scans, so they need a floor.
    if not full() and len(normalized) >= MIN_PREFIX_LENGTH:
        take(_article_ids(normalized, "startswith", limit), "article_prefix")
    if not full() and len(normalized) >= MIN_PARTIAL_LENGTH:
        take(_article_ids(normalized, "contains", limit), "article_partial")

    # 5-7. Name equality is a cheap comparison; prefix and substring get floors.
    if not full():
        take(_name_ids(query, "iexact", limit), "exact_name")
    if not full() and len(query) >= MIN_PREFIX_LENGTH:
        take(_name_ids(query, "istartswith", limit), "name_prefix")
    if not full() and len(query) >= MIN_PARTIAL_LENGTH:
        take(_name_ids(query, "icontains", limit), "name_partial")

    # 8. Typo tolerance last, so a fuzzy name can never outrank an identifier.
    # It is for words: a query with no letter at all is an identifier, which
    # the exact, prefix and substring tiers already cover, so it is skipped.
    if not full() and len(query) >= MIN_FUZZY_LENGTH and _has_letter(query):
        rows = _fuzzy_rows(query, limit)
        take([part_id for part_id, _score in rows], "name_fuzzy", dict(rows))

    return hits


def search_parts(
    raw_query: str | None, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
) -> PartSearchPage:
    """Paginate ranked identities through a bounded, stable window.

    The whole ranked list is capped at ``RESULT_CAP`` identities, so every page
    is a slice of the same deterministic ordering: no part repeats across
    pages and nothing is hydrated before the slice is taken.
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    ranked = search_part_ids(raw_query, limit=RESULT_CAP)
    start = (page - 1) * page_size
    return PartSearchPage(
        hits=ranked[start : start + page_size],
        page=page,
        page_size=page_size,
        total=len(ranked),
        has_more=len(ranked) > start + page_size,
        truncated=len(ranked) >= RESULT_CAP,
    )
