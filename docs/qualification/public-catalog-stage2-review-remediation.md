# Public Catalog Stage 2: remediation qualification

This record qualifies `codex/public-catalog-stage2-review-fixes` against the
independent review. It is evidence only: it neither deploys nor changes a
production database.

## Isolated PostgreSQL 16 dataset

The qualification ran against a newly created local PostgreSQL 16.15 database
(`stage2qual`), migrated from zero to the final migration state. No production
backup or production connection was used. The generator created 125,000 active
parts in 5,000-row batches, each with one unique `ART-000000-XY` article and
English name. It added one RU customs row per part:

- 112,500 confirmed rows, mostly `ПРОКЛАДКА ГОЛОВКИ nnnnn`;
- 12,500 unconfirmed rows, `НЕПОДТВЕРЖДЕННАЯ ПРОКЛАДКА nnnnn`;
- English names distributed between `BEARING DRIVE nnnnn` (90%) and
  `GASKET ASSEMBLY nnnnn` (10%).

This supplies exact, normalized, prefix/substring, typo, no-result and broad
cases without committing generated catalog data. `ANALYZE` ran after loading.

## Normal planner evidence

All plans used `EXPLAIN (ANALYZE, BUFFERS)` with normal scan settings. The
fuzzy plans set only the application-required transaction-local
`pg_trgm.word_similarity_threshold=0.5`; no scan method was disabled.

| Representative predicate | Actual execution | Scan / relevant index | Rows considered / returned |
| --- | ---: | --- | --- |
| exact article | 0.045 ms | Index Scan, `catalog_partnumber_normalized_value_adb8e2df_like` | 1 / 1 |
| article substring | 0.539 ms | Bitmap Index Scan, `catalog_partnumber_normalized_trgm` | 134 candidates, 1 / 1 |
| EN substring | 5.364 ms | Bitmap Index Scan, `catalog_parttype_name_upper_trgm` | 10 candidates, 1 / 1 |
| EN typo `bearng` | 0.602 ms | Seq Scan, 34 filtered before `LIMIT 300` | 334 considered, 300 / 300 |
| RU typo `проклатка` | 12.046 ms | Bitmap Index Scan, `actions_partcustomsinfo_ru_upper_trgm` | confirmed partial-index population; 300 / 300 |

The EN typo is deliberately broad in this synthetic corpus, so PostgreSQL
legitimately selects a fast sequential scan to satisfy the `LIMIT`. The RU
plan uses the partial GIN index and has `Recheck Cond: customs_name_ru_confirmed`:
the 12,500 unconfirmed overlapping rows are structurally absent from that
candidate index.

## Application latency (warm, five repeated samples)

Values are median / maximum milliseconds; the maximum is the small-sample
equivalent percentile. They are observations, not an invented product SLO.

| Query class | Query | Returned | Median / max |
| --- | --- | ---: | ---: |
| exact article | `ART-000001-XY` | 1 | 4.85 / 8.92 |
| partial article | `ART-00001` | 11 | 4.73 / 5.37 |
| exact EN | `BEARING DRIVE 00001` | 300 | 192.71 / 209.23 |
| partial EN | `BEARING` | 300 | 53.70 / 54.05 |
| typo EN | `bearng` | 300 | 132.23 / 189.09 |
| exact RU | `ПРОКЛАДКА ГОЛОВКИ 00001` | 300 | 777.40 / 793.60 |
| partial RU | `ПРОКЛАДКА` | 300 | 55.36 / 57.64 |
| typo RU | `проклатка` | 300 | 554.04 / 564.07 |
| no result | `NO-SUCH-TERM` | 0 | 5.55 / 6.24 |
| allowed broad | `GASKET` | 300 | 15.09 / 15.62 |

The repeated-name corpus intentionally makes the name examples broad and
therefore exercises the result cap. Exact name queries can still flow through
lower tiers to fill that cap; this is current Stage 2 behaviour, retained per
the remediation scope rather than redesigned here.

## Regression and migration checks

- SQLite targeted portable search and Stage 1 contract tests: 72 passed.
- PostgreSQL targeted search, Stage 1 contract and index tests: 94 passed.
- `makemigrations --check --dry-run` and `manage.py check`: passed.
- Fresh PostgreSQL 16 migration 0 to latest: passed; it installed `pg_trgm`
  and the confirmed-only RU partial GIN index.
- Exact Stage 1 base `12398646a515765aa6af4b2b9f0c77ad4fd23c0c` schema to the
  final candidate: passed. It applied `catalog.0007`, `actions.0013` and
  `actions.0014`; inspection found all three intended GIN indexes and the RU
  `WHERE customs_name_ru_confirmed` predicate.

The test matrix records search-only, hydration-only and combined query counts
for no-stock and real bulk-stock results at 1, 20 and 50. Stage 1 scaling also
now uses separate like-for-like bulk, reserved-bulk and serial families at
1/20/50, so differing inventory branches cannot invalidate a scale comparison.
