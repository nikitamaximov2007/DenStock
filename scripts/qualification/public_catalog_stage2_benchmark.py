"""Stage 2 Search 2.0 qualification harness for an isolated PostgreSQL 16 corpus.

Run it only against a disposable database that
``generate_public_catalog_stage2_qualification`` has filled:

    DATABASE_URL=postgres://user:pass@127.0.0.1:PORT/corpus \
        python scripts/qualification/public_catalog_stage2_benchmark.py \
        --confirm-isolated --expect-database corpus --output evidence.json

What it measures, and why each part exists:

* ``validity`` - every benchmark term is first run through the real service and
  must land on its intended target and tier. A benchmark over a term that does
  not do what its label says is exactly the mistake the first corpus made.
* ``explain`` - every SELECT that Search 2.0 actually issues for a case is
  captured and re-run under ``EXPLAIN (ANALYZE, BUFFERS)`` with the planner left
  alone. The only setting applied is the one the application itself applies:
  the transaction-local ``pg_trgm.word_similarity_threshold`` for the fuzzy SQL.
* ``latency`` - wall-clock of the public paginated call, warmed, repeated.
* ``fast_path`` / ``guc`` / ``pure_read`` - the invariants a reviewer needs to
  see rather than take on trust.

Nothing here writes business data: the harness refuses non-loopback hosts and
checks table row counts before and after.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
BUSINESS_TABLES = (
    "catalog_parttype",
    "catalog_partnumber",
    "actions_partcustomsinfo",
    "inventory_stocklot",
    "inventory_partitem",
    "inventory_stockmovement",
    "sales_reservation",
    "sales_reservationline",
)

# The twelve required classes. Each carries the identity it must resolve to,
# so the benchmark can refuse to measure a term that does not mean what the
# label says. ``target`` is an article of a seeded row; ``None`` means the case
# is about volume or absence rather than one identity.
CASES = (
    ("1", "exact article", "420-892-388", "420-892-388", "exact_article"),
    ("2", "normalized exact article", "420892388", "420-892-388", "normalized_exact_article"),
    ("3", "article prefix", "4208", "420-892-388", "article_prefix"),
    ("4", "article substring", "8923", "420-892-388", "article_partial"),
    ("5", "exact EN", "QUALIFICATION EXACT EN NAME", "Q-EN-EXACT-01", "exact_name"),
    ("6", "EN partial", "EXACT EN", "Q-EN-EXACT-01", "name_partial"),
    ("7", "EN typo", "bearng", "Q-BRG-01", "name_fuzzy"),
    ("8", "exact confirmed RU", "УНИКАЛЬНОЕ ТОЧНОЕ РУ НАЗВАНИЕ", "Q-EN-EXACT-01", "exact_name"),
    ("9", "RU partial", "ТОЧНОЕ РУ", "Q-EN-EXACT-01", "name_partial"),
    ("10", "RU typo", "проклатка", "Q-GSK-01", "name_fuzzy"),
    ("11", "no result", "ZXQJW", None, None),
    ("12", "broad allowed", "GASKET", None, "name_prefix"),
)
EXTRA_PROBES = (
    ("unconfirmed RU exact text", "НЕПОДТВЕРЖДЕННАЯ ПРОКЛАДКА", "Q-RU-UNC-01"),
    ("ranking: exact article over fuzzy-looking name", "BERRNG-01", "BERRNG-01"),
)

TIER_PATTERNS = (
    ("name_fuzzy", re.compile(r"WORD_SIMILARITY\(", re.I)),
    ("article_exact", re.compile(r'"normalized_value"\s*=', re.I)),
    ("article_prefix", re.compile(r"\"normalized_value\"(::text)?\s+LIKE\s+'[^%']+%'", re.I)),
    ("article_partial", re.compile(r"\"normalized_value\"(::text)?\s+LIKE\s+'%", re.I)),
    ("name_exact", re.compile(r"UPPER\([^)]*\)\s*=\s*UPPER\(", re.I)),
    ("name_prefix", re.compile(r"LIKE\s+UPPER\('[^%']+%'\)", re.I)),
    ("name_partial", re.compile(r"LIKE\s+UPPER\('%", re.I)),
)


def _tier_of(sql: str) -> str:
    source = "ru" if "actions_partcustomsinfo" in sql else "en"
    for tier, pattern in TIER_PATTERNS:
        if pattern.search(sql):
            if tier.startswith("name_") and tier != "name_fuzzy":
                return f"{tier}_{source}"
            return tier
    return "other"


def _guard(args):
    from django.conf import settings
    from django.db import connection

    database = settings.DATABASES["default"]
    if not args.confirm_isolated:
        raise SystemExit("Refusing to run without --confirm-isolated.")
    if connection.vendor != "postgresql":
        raise SystemExit("This harness needs PostgreSQL 16.")
    if (database.get("HOST") or "") not in LOOPBACK:
        raise SystemExit(f"Refusing a non-loopback database host: {database.get('HOST')!r}")
    if database.get("NAME") != args.expect_database:
        raise SystemExit(
            f"Connected to {database.get('NAME')!r}, expected {args.expect_database!r}."
        )


def _scalar(sql, params=None):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(sql, params or [])
        return cursor.fetchone()[0]


def _row_counts():
    return {table: _scalar(f"SELECT count(*) FROM {table}") for table in BUSINESS_TABLES}


def _targets():
    from apps.catalog.models import PartNumber

    wanted = {case[3] for case in CASES if case[3]} | {probe[2] for probe in EXTRA_PROBES}
    return dict(
        PartNumber.objects.filter(value__in=wanted).values_list("value", "part_id")
    )


# --- validity -------------------------------------------------------------------------


def validity(targets):
    from apps.catalog.search import search_part_ids, search_parts

    results = []
    for number, label, query, target, tier in CASES:
        hits = search_part_ids(query)
        page = search_parts(query, page=1, page_size=20)
        target_id = targets.get(target) if target else None
        first = hits[0] if hits else None
        tiers = {}
        for hit in hits:
            tiers[hit.match_type] = tiers.get(hit.match_type, 0) + 1
        target_hit = next((hit for hit in hits if hit.part_id == target_id), None)
        if target is None and tier is None:
            ok = not hits
        elif target is None:
            ok = bool(hits) and first.match_type == tier
        else:
            ok = target_hit is not None and target_hit.match_type == tier
            if tier in {"exact_article", "normalized_exact_article", "exact_name"}:
                ok = ok and first.part_id == target_id
        results.append({
            "case": number, "label": label, "query": query, "target_article": target,
            "target_part_id": target_id, "hits": len(hits), "page_total": page.total,
            "first_part_id": first.part_id if first else None,
            "first_match_type": first.match_type if first else None,
            "target_match_type": target_hit.match_type if target_hit else None,
            "target_position": hits.index(target_hit) + 1 if target_hit else None,
            "tiers": tiers, "valid": ok,
        })
    extras = []
    for label, query, article in EXTRA_PROBES:
        hits = search_part_ids(query)
        part_id = targets.get(article)
        extras.append({
            "label": label, "query": query, "article": article, "hits": len(hits),
            "target_present": any(hit.part_id == part_id for hit in hits),
            "first_match_type": hits[0].match_type if hits else None,
            "first_is_target": bool(hits) and hits[0].part_id == part_id,
        })
    return results, extras


def normalized_integrity():
    from apps.catalog.models import PartNumber, normalize_number

    bad = 0
    total = 0
    for value, normalized in PartNumber.objects.values_list("value", "normalized_value").iterator(
        chunk_size=10_000
    ):
        total += 1
        bad += normalize_number(value) != normalized
    return {"checked": total, "mismatched": bad}


# --- explain --------------------------------------------------------------------------


def _walk(node, acc):
    acc["nodes"].append(node.get("Node Type"))
    if node.get("Index Name"):
        acc["indexes"].add(node["Index Name"])
    if node.get("Relation Name"):
        acc["relations"].add(node["Relation Name"])
    for key, target in (
        ("Rows Removed by Filter", "removed_filter"),
        ("Rows Removed by Index Recheck", "removed_recheck"),
    ):
        acc[target] += int(node.get(key, 0) or 0)
    if node.get("Recheck Cond"):
        acc["recheck"].append(node["Recheck Cond"])
    if node.get("Filter"):
        acc["filters"].append(node["Filter"])
    for child in node.get("Plans", []) or []:
        _walk(child, acc)


def _explain(sql, *, fuzzy):
    from django.db import connection, transaction

    from apps.catalog.search import WORD_SIMILARITY_THRESHOLD

    with transaction.atomic(), connection.cursor() as cursor:
        if fuzzy:
            # The one setting the application applies itself, exactly as it does.
            cursor.execute(
                "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                [str(WORD_SIMILARITY_THRESHOLD)],
            )
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
        document = cursor.fetchone()[0]
    if isinstance(document, str):
        document = json.loads(document)
    root = document[0]
    plan = root["Plan"]
    acc = {"nodes": [], "indexes": set(), "relations": set(), "removed_filter": 0,
           "removed_recheck": 0, "recheck": [], "filters": []}
    _walk(plan, acc)
    return {
        "planning_ms": round(root.get("Planning Time", 0.0), 3),
        "execution_ms": round(root.get("Execution Time", 0.0), 3),
        "top_node": plan.get("Node Type"),
        "scan_nodes": [n for n in acc["nodes"] if n and "Scan" in n],
        "indexes": sorted(acc["indexes"]),
        "relations": sorted(acc["relations"]),
        "actual_rows": plan.get("Actual Rows"),
        "rows_removed_by_filter": acc["removed_filter"],
        "rows_removed_by_recheck": acc["removed_recheck"],
        "recheck_cond": acc["recheck"],
        "filters": acc["filters"],
        "shared_hit": plan.get("Shared Hit Blocks"),
        "shared_read": plan.get("Shared Read Blocks"),
    }


def explain_matrix():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.catalog.search import search_part_ids

    matrix = []
    for number, label, query, _target, _tier in CASES:
        with CaptureQueriesContext(connection) as captured:
            hits = search_part_ids(query)
        statements = []
        for entry in captured.captured_queries:
            sql = entry["sql"].strip()
            upper = sql.upper()
            if not upper.startswith("SELECT"):
                continue
            if "SET_CONFIG" in upper or "CURRENT_SETTING" in upper:
                continue
            tier = _tier_of(sql)
            detail = _explain(sql, fuzzy=tier == "name_fuzzy")
            detail["tier"] = tier
            detail["sql"] = sql
            statements.append(detail)
        matrix.append({
            "case": number, "label": label, "query": query, "hits": len(hits),
            "statements_total": len(captured.captured_queries),
            "selects_explained": len(statements),
            "sum_execution_ms": round(sum(s["execution_ms"] for s in statements), 3),
            "sum_planning_ms": round(sum(s["planning_ms"] for s in statements), 3),
            "statements": statements,
        })
    return matrix


# --- latency --------------------------------------------------------------------------


def latency(samples, warmup):
    from apps.catalog.search import search_parts

    out = []
    for number, label, query, _target, _tier in CASES:
        for _ in range(warmup):
            search_parts(query, page=1, page_size=20)
        timings = []
        total = None
        for _ in range(samples):
            started = time.perf_counter()
            page = search_parts(query, page=1, page_size=20)
            timings.append((time.perf_counter() - started) * 1000)
            total = page.total
        ordered = sorted(timings)
        p95 = ordered[max(0, int(round(0.95 * len(ordered))) - 1)]
        out.append({
            "case": number, "label": label, "query": query, "result_total": total,
            "samples": samples, "median_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(p95, 3), "min_ms": round(ordered[0], 3),
            "max_ms": round(ordered[-1], 3),
        })
    return out


# --- fast path, GUC, pure read ----------------------------------------------------------


def fast_path():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.catalog.search import search_part_ids

    query = "420892388"
    with CaptureQueriesContext(connection) as captured:
        hits = search_part_ids(query)
    sqls = [entry["sql"] for entry in captured.captured_queries]
    joined = "\n".join(sqls).upper()
    return {
        "query": query,
        "hits": len(hits),
        "first_match_type": hits[0].match_type if hits else None,
        "query_count": len(sqls),
        "tiers_executed": [_tier_of(sql) for sql in sqls],
        "uses_word_similarity": "WORD_SIMILARITY" in joined,
        "uses_trgm_operator": "<%" in joined,
        "sets_guc": "SET_CONFIG" in joined,
        "reads_guc": "CURRENT_SETTING" in joined,
        "sql": sqls,
    }


def _threshold():
    # ``missing_ok``: pg_trgm registers this setting only once its library is
    # loaded into the backend, so a fresh connection has no such parameter yet.
    return _scalar("SELECT current_setting('pg_trgm.word_similarity_threshold', true)")


def _load_trgm():
    """Load pg_trgm into this backend with a pure read, so the setting exists."""
    _scalar("SELECT show_trgm('x')")


def guc_safety():
    from django.db import connection, transaction

    import apps.catalog.search as search

    # A brand-new backend first: the public entry point must work even before
    # pg_trgm has been loaded, in autocommit and inside an outer transaction.
    connection.close()
    results = {"fresh_backend_setting_before_search": _threshold()}
    search.search_part_ids("bearng")
    results["fresh_backend_autocommit_search"] = "ok"
    connection.close()
    with transaction.atomic():
        search.search_part_ids("bearng")
    results["fresh_backend_atomic_search"] = "ok"

    _load_trgm()
    baseline = _threshold()
    results["baseline"] = baseline

    search.search_part_ids("bearng")
    results["autocommit"] = _threshold()

    with transaction.atomic():
        search.search_part_ids("bearng")
        results["inside_atomic_after_search"] = _threshold()
    results["after_atomic"] = _threshold()

    with transaction.atomic():
        with transaction.atomic():
            search.search_part_ids("bearng")
            results["inside_nested_after_search"] = _threshold()
        results["outer_after_nested"] = _threshold()
    results["after_nested"] = _threshold()

    original = search._FUZZY_SQL
    search._FUZZY_SQL = "SELECT this_function_does_not_exist(%s, %s, %s, %s, %s)"
    try:
        for mode in ("autocommit", "atomic"):
            try:
                if mode == "atomic":
                    with transaction.atomic():
                        try:
                            search.search_part_ids("bearng")
                        finally:
                            results["exception_inside_atomic_seen"] = _threshold()
                else:
                    search.search_part_ids("bearng")
            except Exception as exc:  # noqa: BLE001 - the failure is the point
                results[f"exception_{mode}_raised"] = type(exc).__name__
            results[f"after_exception_{mode}"] = _threshold()
    finally:
        search._FUZZY_SQL = original
    results["leak_free"] = all(
        results[key] == baseline
        for key in ("autocommit", "after_atomic", "outer_after_nested", "after_nested",
                    "after_exception_autocommit", "after_exception_atomic")
    )
    return results


def pure_read():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.catalog.search import search_part_ids, search_parts

    write = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|ALTER|DROP|CREATE)\b", re.I)
    before = _row_counts()
    with CaptureQueriesContext(connection) as captured:
        for _number, _label, query, _target, _tier in CASES:
            search_part_ids(query)
            search_parts(query, page=2, page_size=20)
    after = _row_counts()
    writes = [entry["sql"] for entry in captured.captured_queries if write.match(entry["sql"])]
    gucs = [
        entry["sql"] for entry in captured.captured_queries
        if "SET_CONFIG" in entry["sql"].upper()
    ]
    return {
        "statements": len(captured.captured_queries),
        "write_statements": writes,
        "guc_statements": len(gucs),
        "row_counts_unchanged": before == after,
        "row_counts": after,
    }


# Typos whose fuzzy candidate set differs by two orders of magnitude. The corpus
# gives one QUALIFICATION name, ~12.5k GASKET names and ~112.5k BEARING names,
# so the same tier can be timed against 1, ~12.5k and ~112.5k real candidates.
FUZZY_SCALING = (("qualifcation", "QUALIFICATION"), ("gaskt", "GASKET"), ("bearng", "BEARING"))


def fuzzy_scaling(samples, warmup):
    from django.db import connection, transaction

    from apps.catalog.search import WORD_SIMILARITY_THRESHOLD, search_parts

    out = []
    for typo, word in FUZZY_SCALING:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                [str(WORD_SIMILARITY_THRESHOLD)],
            )
            cursor.execute(
                "SELECT count(*) FROM catalog_parttype WHERE UPPER(%s) <%% UPPER(name::text)",
                [typo],
            )
            candidates = cursor.fetchone()[0]
        for _ in range(warmup):
            search_parts(typo, page=1, page_size=20)
        timings = []
        for _ in range(samples):
            started = time.perf_counter()
            search_parts(typo, page=1, page_size=20)
            timings.append((time.perf_counter() - started) * 1000)
        ordered = sorted(timings)
        median = statistics.median(ordered)
        out.append({
            "typo": typo, "intended_word": word, "fuzzy_candidates": candidates,
            "samples": samples, "median_ms": round(median, 3),
            "p95_ms": round(ordered[max(0, int(round(0.95 * len(ordered))) - 1)], 3),
            "per_1k_candidates_ms": round(median / max(candidates, 1) * 1000, 3),
        })
    return out


def _write(path, evidence):
    Path(path).write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-isolated", action="store_true")
    parser.add_argument("--expect-database", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    args = parser.parse_args()

    import django

    django.setup()
    _guard(args)

    url = os.environ.get("DATABASE_URL", "")
    evidence = {
        "database": urlparse(url).path.lstrip("/"),
        "server_version": _scalar("SELECT version()"),
        "settings": {
            name: _scalar(f"SELECT current_setting('{name}', true)")
            for name in ("enable_seqscan", "enable_indexscan", "enable_bitmapscan",
                         "random_page_cost", "work_mem", "shared_buffers",
                         "pg_trgm.word_similarity_threshold")
        },
    }
    targets = _targets()
    evidence["targets"] = targets
    evidence["normalized_integrity"] = normalized_integrity()
    evidence["validity"], evidence["extra_probes"] = validity(targets)
    if not all(case["valid"] for case in evidence["validity"]):
        _write(args.output, evidence)
        bad = [case["case"] for case in evidence["validity"] if not case["valid"]]
        raise SystemExit(f"Corpus validity gate FAILED for cases {bad}; nothing benchmarked.")
    evidence["fast_path"] = fast_path()
    evidence["guc"] = guc_safety()
    evidence["pure_read"] = pure_read()
    evidence["explain"] = explain_matrix()
    evidence["latency"] = latency(args.samples, args.warmup)
    evidence["fuzzy_scaling"] = fuzzy_scaling(args.samples, args.warmup)
    _write(args.output, evidence)
    print(f"Evidence written to {args.output}")


if __name__ == "__main__":
    main()
