# Public Catalog Stage 2: remediation qualification

This record tracks qualification of `codex/public-catalog-stage2-review-fixes`
against the independent review. It is evidence only: it neither deploys nor
changes a production database.

> The final PostgreSQL 16 qualification is in
> [`public-catalog-stage2-pg16-final.md`](public-catalog-stage2-pg16-final.md).
> It supersedes the planner and latency figures below, which stay only as
> history of the invalid first corpus.

## Qualification history: invalid first corpus

The first attempted corpus had the requested raw row counts, but it was not a
valid Search 2.0 qualification corpus. The generator stored generated names
with six-digit suffixes, for example `BEARING DRIVE 000001`, while the report
called five-digit probes such as `BEARING DRIVE 00001` exact-name cases. They
were not exact rows. Search correctly continued through its lower substring
and fuzzy tiers, creating broad capped results rather than the named expected
identity. Thus the evidence labelled "exact EN" and "exact RU" did not prove
the actual path. This is a fixture/probe mismatch, not a demonstrated Search
2.0 production defect.

The old attempt remains recorded below for audit. Its planner and latency
numbers must not be used as final qualification evidence.

## Recovered fixture and preflight

`generate_public_catalog_stage2_qualification` is a deliberately guarded
management command. It requires `--confirm-isolated`, refuses any database
that already has a `PartType`, and is intended only after migration of an
isolated PostgreSQL 16 database. It creates explicit normal ORM seed rows for
the deterministic probes, then uses bulk creation only for the large body.

Bulk-created `PartNumber` rows explicitly set `normalized_value` with the
same `catalog.normalize_number` used by `PartNumber.save`. `PartType` has no
save-derived search fields. Every searchable body part has a canonical
ARTICLE `PartNumber`, an active `PartType`, and a one-to-one
`PartCustomsInfo`; confirmed and unconfirmed Russian rows use the real flag.
There is no stock, deliberately proving that zero stock does not hide a
searchable part.

The miniature 20-row corpus is enforced by
`tests/test_public_catalog_qualification_fixture.py` against the shared
`search_part_ids` service before a large corpus is permitted as evidence. It
checks exact and normalized `420-892-388`, article prefix and substring,
isolated exact `BEARING`, confirmed `ПРОКЛАДКА`, unconfirmed duplicate
exclusion, and exact-article ranking over a fuzzy-looking name. On PostgreSQL
with `pg_trgm`, it additionally checks `bearng` and `проклатка` through the
real fuzzy tier.

On 2026-09-10 the corrected command was run on a fresh disposable PostgreSQL
16.15 database, migrated from zero. It created exactly 125,000 `PartType`,
125,000 canonical ARTICLE `PartNumber`, 125,000 populated English names,
125,000 `PartCustomsInfo`, 112,500 confirmed RU rows, and 12,500 unconfirmed
RU rows. The live service preflight returned the expected first hit and tier:

| Probe | Returned | Tier |
| --- | ---: | --- |
| `420-892-388` | 1 | exact article |
| `420892388` | 1 | normalized exact article |
| `4208` | 24 | article prefix, target first |
| `8923` | 23 | article substring, target first |
| `QUALIFICATION EXACT EN NAME` | 1 | exact name |
| `bearng` | 300 | name fuzzy, `BEARING` target included |
| `УНИКАЛЬНОЕ ТОЧНОЕ РУ НАЗВАНИЕ` | 1 | exact name |
| `проклатка` | 300 | name fuzzy, `ПРОКЛАДКА` target included |
| `НЕПОДТВЕРЖДЕННАЯ ПРОКЛАДКА` | 0 | excluded |
| `BERRNG-01` | 1 | exact article |

## Actual Search 2.0 data path

| Tier | Persisted source and condition |
| --- | --- |
| Exact article | `catalog_partnumber.normalized_value`, `kind IN (oem, article)`; Python splits raw `value` equality from normalized-only equality |
| Normalized article | Same indexed `normalized_value` lookup and canonical `normalize_number` |
| Article prefix | `normalized_value__startswith`, same canonical kinds |
| Article substring | `normalized_value__contains`, same canonical kinds |
| EN exact/prefix/substring | `catalog_parttype.name` through `iexact`/`istartswith`/`icontains` |
| EN fuzzy | PostgreSQL raw SQL over `catalog_parttype.name`, `UPPER(name::text) <%` and `word_similarity` |
| Confirmed RU exact/prefix/substring | `actions_partcustomsinfo.customs_name_ru` only where `customs_name_ru_confirmed=True` |
| Confirmed RU fuzzy | PostgreSQL raw SQL over the same confirmed `PartCustomsInfo` predicate |

`PartCustomsInfo.part_type` is a one-to-one relation. Search has no
`is_active`, manufacturer, unit, public-visibility, stock, annotation, or
hydration filter. It first returns bounded part IDs only. PostgreSQL uses GIN
trigram indexes for normalized articles and upper-cased English names, plus
the confirmed-only partial upper-cased Russian index. Exact `normalized_value`
uses Django's ordinary B-tree index.

## Archived invalid PostgreSQL 16 attempt (not acceptance evidence)

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
