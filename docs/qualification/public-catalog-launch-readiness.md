# Public catalog launch readiness: qualification evidence

Branch `claude/public-catalog-launch-readiness`, based on the Stage 11 preview
candidate `ebd79727866c126f9b1a9eb41513dc4531f52d3c`. Everything below ran on
an isolated workstation: a dedicated `postgres:16` container bound to
127.0.0.1, local worktrees and a local copy of the production catalog. No
production host, database, backup or Caddy was touched, and nothing was
deployed or merged. The preview was only read with a handful of GET requests
during the audit.

## Environment

| Item | Value |
| --- | --- |
| Machine | Apple M4, 10 cores, macOS |
| PostgreSQL | 16.15 (Debian 16.15-1.pgdg13+2) aarch64, Docker, `shared_buffers=128MB`, `work_mem=4MB` |
| Python / Django / Pillow | 3.12.14 / 5.2.17 / 12.3.0 |
| Round trip `SELECT 1` | 0.2 ms |

## Audit findings on the base (fixed in this branch)

| # | Finding on `ebd7972` | Fix |
| --- | --- | --- |
| 1 | `/search/?page=abc` answered HTTP 500 (reproduced once on the preview) | `parse_page`, ASCII-only digits |
| 2 | Filters applied to the current page only; totals, facets and pages disagreed; no page links existed | filters over the whole ranked list, pager, shareable URLs |
| 3 | Search listed non-public and retired parts | one visibility rule `is_public and is_active` |
| 4 | Manufacturer filter compared the card FK name while cards showed `manufacturer_display` (BRP/Polaris) | filter uses `manufacturer_display` |
| 5 | Application filter compared `ГИДРОЦИКЛ` to vehicle type names like `Гидроцикл`, so it never matched | explicit application area plus compatibility through the canonical mapping |
| 6 | Only one analog direction on the part page; no way to unpublish a confirmed analog without deleting the link | both directions; "Снять с каталога" |
| 7 | "Узнать о поставке" was plain text, a dead end; cart silently refused zero-stock parts | supply-inquiry cart lines |
| 8 | Cart refusals were silent redirects | messages with the reason and the part name |
| 9 | JSON 404, internal 500 page (DenisStock branding, internal URL names), default CSRF page | public HTML error pages; a 503 page for database failures |
| 10 | Canonical links relative; sitemap loaded all 126k IDs per request; robots allowed everything while the preview relied on the edge for noindex | absolute canonical from `PUBLIC_CATALOG_BASE_URL`, sitemap index with 10k files, env-driven indexing defaulting to noindex |
| 11 | No CSP, HTML cacheable by default, no access log | CSP without scripts, private no-store HTML, one access line per request |
| 12 | Role script in Git lacked grants for analogs, compatibility and photos | exact read graph, RLS on photos, read-only role defaults |
| 13 | Public process had no static assets (inline CSS only) | public-only finder-served stylesheet; internal and admin static unreachable |
| 14 | `test_part_search.py::test_hits_hydrate_through_stage1_public_facts_in_rank_order` failed since Stage 4 | compares public IDs |
| 15 | `catalog.0008` took 1,903 s on the real catalog (see PG16 upgrade) and left the old release unable to insert parts | set-based backfill; database defaults in `0011` |
| 16 | `DJANGO_SECURE_COOKIES` read without a boolean cast | boolean, Secure by default on the public runtime |
| 17 | 2 sync workers saturate at about 60 req/s | 3 threaded workers x 2, persistent connections |

## PG16 fresh migration

Empty database, candidate code: 120 migrations applied, 0 unapplied,
`makemigrations --check` reports no changes. Present with the expected
definitions: `catalog_parttype_public_id_key`, `uniq_part_analog_pair`,
`part_analog_not_self`, the three trigram indexes
(`catalog_partnumber_normalized_trgm`, `catalog_parttype_name_upper_trgm`,
`actions_partcustomsinfo_ru_upper_trgm WHERE customs_name_ru_confirmed`),
`public_photo_part_status_idx`, `uniq_public_photo_primary` (partial:
`is_primary AND status = 'published'`), `uniq_public_photo_variant`,
`public_photo_published_has_provenance`, `public_photo_primary_is_published`.

## PG16 upgrade migration

**From the exact base `ebd7972`** (worktree at that SHA): 119 migrations,
then representative data through the base code's canonical services: 31
parts (one non-public), a BRP-promoted part, customs rows with confirmed
and unconfirmed Russian names and an application area, 3 received lots, an
active reservation, one confirmed and one unconfirmed analog link, an
explicit compatibility, and 3 historical internal photos without
provenance. Upgrading to the candidate applied `catalog.0010` (and `0011`
on the later rerun) only. Fingerprints (count and md5 over key columns of
parts, numbers, analog links, internal photos, customs rows, lots,
reservation lines, reservations, compatibility and stock balance cache)
are identical before and after. `catalog_publicpartphoto` has 0 rows. With
the role script applied, the public runtime under the restricted role shows
no photo for the part with legacy photos, the confirmed analog only, and
hides the non-public part. HTTP acceptance: 89 checks, 0 failed.

**From the production schema on real data.** A local copy of the production
database from 2026-09-06 (125,981 parts, 110 migrations) was restored twice
into the isolated container and migrated to the candidate:

| Migration | Stage 4 code as on `ebd7972` | This branch |
| --- | ---: | ---: |
| `catalog.0007_search_trigram` | 0.747 s | 0.794 s |
| `actions.0013` + `actions.0014` | 0.049 s | 0.046 s |
| `catalog.0008_parttype_public_identity` | **1,903.316 s** | **1.967 s** |
| `catalog.0009` + `catalog.0010` | 0.391 s | 0.279 s |
| `catalog.0011_public_catalog_rollback_defaults` | n/a | 0.024 s |
| whole `migrate` command | 3,773 s | 4 s |
| `business_generation` increase | +125,988 | +8 |

Both runs: fingerprints over parts, numbers, analog links, internal photos,
customs rows, lots, serial items, reservation lines, sales, movements and
customers identical before and after; 125,981 distinct non-null public IDs;
0 public photos. The rehearsal copy also carried older migrations
(`customs_orders`, `ordered_parts`) that production already has; the real
release applies only the catalog and actions migrations listed in the
release runbook. On the migrated copy the restricted role passed the full
HTTP acceptance (89 checks, 0 failed, including the cart round trip).

The migration round trip and the old-release INSERT are pinned by
`tests/test_public_catalog_migrations.py` (SQLite and PG16).

## Query counts

Application queries per public request, captured by the test suite
(recorded as JUnit properties). Flat from 1 to 50 means no per-row query.

| Surface | Backend | 1 | 20 | 50 |
| --- | --- | ---: | ---: | ---: |
| Search page view (filters, facets, photos, relations, one page of cards) | SQLite | 23 | 23 | 23 |
| Search page view | PG16 (in a test transaction) | 29 | 29 | 29 |
| Catalog service with manufacturer + analog + in-stock filters | SQLite | 18* | 23 | 23 |
| Catalog service with filters | PG16 | 24* | 29 | 29 |
| Part page with N confirmed relations, each with stock and a photo | both | 22 | 22 | 22 |
| Cart page with N lines | both | 12 | 12 | 12 |
| Primary photos for N parts | both | 1 | 1 | 1 |
| Stage 1 hydration (`build_public_part_facts`), from Stage 2 evidence | both | 8 | 8 | 8 |

\* no result matched the filter at N = 1, so no card was hydrated.

PostgreSQL adds six statements to the search in a test transaction
(savepoint, threshold read, `set_config`, fuzzy query, restore, release);
in autocommit production the fuzzy block is four statements. All captured
statements are reads.

## 125k performance regression

Stage 2's generator built the valid 125k corpus in a fresh database; the
launch benchmark then added 300 stocked parts, 400 analog links (300
confirmed), 300 published photos, 8 manufacturers across all parts and
5,000 application areas, ran `VACUUM ANALYZE`, and measured 30 samples per
term after 5 warm-ups. Median ms (p95 in brackets where useful):

| # | Class | Query | Hits | Search 2.0 | Catalog service | With filters | Full page | Part page |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | exact article | `420-892-388` | 1 | 3.2 | 10.2 | 7.3 | 11.2 | 7.6 |
| 2 | normalized article | `420892388` | 1 | 3.2 | 10.8 | 7.4 | 11.5 | 7.7 |
| 3 | article prefix | `4208` | 24 | 4.2 | 12.4 | 8.6 | 15.6 | 8.0 |
| 4 | article substring | `8923` | 23 | 3.3 | 12.5 | 8.3 | 14.9 | 8.3 |
| 5 | exact EN | `QUALIFICATION EXACT EN NAME` | 1 | 4.7 | 12.6 | 9.0 | 13.0 | 7.9 |
| 6 | EN partial | `EXACT EN` | 1 | 4.2 | 13.3 | 9.0 | 12.3 | 7.9 |
| 7 | EN typo | `bearng` | 300 | 147.9 (156.8) | 201.4 | 201.1 | 205.5 (221.6) | 7.9 |
| 8 | exact confirmed RU | `УНИКАЛЬНОЕ ТОЧНОЕ РУ НАЗВАНИЕ` | 1 | 6.0 | 11.8 | 9.0 | 13.6 | 8.6 |
| 9 | RU partial | `ТОЧНОЕ РУ` | 1 | 5.2 | 11.6 | 8.4 | 12.4 | 8.0 |
| 10 | RU typo | `проклатка` | 300 | 554.2 (563.7) | 608.6 | 605.8 | 613.7 (625.8) | 7.8 |
| 11 | no result | `ZXQJW` | 0 | 4.4 | 5.6 | 5.7 | 6.5 | |
| 12 | broad | `GASKET` | 300 | 16.7 | 35.0 | 30.9 | 35.7 | 7.8 |

Search 2.0 numbers match the Stage 2 record (exact 5.0, `bearng` 135,
`проклатка` 543, `GASKET` 16.0 ms there): the search itself is unchanged.
The public layer adds about 7 ms to a single-result page and 20 to 55 ms
when 300 identities must be classified for facets; most of that is the
canonical `live_stock_rows` read for 300 stocked parts (23 ms measured
alone). Cases 7 and 10 remain the adversarial Stage 2 workloads where 90%
of the catalog shares the mistyped word. The measurement phase changed no
row count (`pure_read: true`).

On the real-data copy (126k aftermarket and warehouse parts), single request
medians through HTTP: `420892388` 15 ms, `420-892-388` 18 ms, `spark plug`
33 ms, `piston kit` 36 ms, `bearing` 39 ms, `pistn` 84 ms, `gaskt` 96 ms,
`4208` 124 ms. `4208` spends 70 ms inside Search 2.0 (two article tiers of
34 ms each over the real number set, which includes internal reference
numbers); that is Search 2.0 behaviour on real data and a candidate for a
separate Search 2.0 tuning task, not a regression of this branch.

## Load

`scripts/qualification/public_catalog_load.py`: loopback only, fixed mix
(home 10, exact 15, normalized 10, partial 10, typo 5, filtered page 2 10,
part page 25, cart read 5, cart update 5, photo 5 when discovered). Public
settings, restricted role, 125k corpus unless noted.

| Runtime | Clients | Seconds | Requests | req/s | Errors | Home p50 | Exact p50 / p95 | Part p50 / p95 | Typo p50 | Cart update p50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 sync workers (base config) | 4 | 45 | 2,734 | 60.7 | 0 | 24.7 | 44.0 / 90.2 | 43.7 / 93.3 | 238.2 | 83.6 |
| 2 sync workers | 8 | 45 | 2,794 | 61.9 | 0 | 75.8 | 93.0 / 233.7 | 94.1 / 274.4 | 279.3 | 189.5 |
| 2 sync workers | 16 | 45 | 2,711 | 59.9 | 0 | 210.7 | 239.0 / 406.6 | 214.9 / 391.6 | 409.0 | 435.7 |
| 3 x 2 threads | 8 | 45 | 4,931 | 109.0 | 0 | 9.8 | 54.8 / 121.3 | 51.5 / 114.3 | 294.2 | 96.8 |
| 3 x 2 threads, persistent DB connections | 8 | 45 | 5,863 | 129.8 | 0 | 7.8 | 42.3 / 97.6 | 38.8 / 92.4 | 294.3 | 65.8 |
| 3 x 2 threads, persistent | 16 | 45 | 5,542 | 122.7 | 0 | 51.9 | 90.5 / 290.4 | 90.2 / 304.7 | 363.8 | 158.7 |
| final config, photos in the mix | 8 | 90 | 10,580 | 117.5 | 0 | 3.0 | 35.2 / 134.8 | 39.7 / 144.6 | 286.7 | 65.9 |
| final config, real-data copy | 8 | 60 | 7,338 | 122.2 | 0 | 9.9 | 46.7 / 93.1 | 34.5 / 78.3 | 128.1 | 71.6 |

Observed: no lock waits (`pg_locks` not granted: 0 at every sample), at
most 6 connections of the public role, worker memory flat at about 70 MB
after 10,580 requests, no 5xx. One run with `--max-requests` recycling
produced 4 client-side disconnects when a worker restarted with open
keep-alive connections; recycling was therefore not adopted.

## Security and boundary

* Route matrix under the real public middleware: 35 internal paths return
  404 for GET and POST, no redirect to a login, no DenisStock markup
  (`tests/test_public_runtime_boundary.py`).
* Restricted role on PG16 (`tests/test_public_catalog_role_postgresql.py`):
  every public page and service works under `SET LOCAL ROLE`; 20 forbidden
  statements (writes to catalog, photos, stock, reservations, customs;
  reads of users, customers, sales, repairs, batch lines, suppliers,
  internal photos, sessions, stock balance cache; DDL) fail with
  "permission denied"; direct SQL sees only published photo rows; the
  granted table set equals the documented list exactly.
* Cart tampering (`tests/test_public_catalog_cart.py`): client price and
  extra fields ignored, invalid, huge and non-ASCII quantities refused,
  quantity above availability refused with a reason, 50-line cap, corrupt
  and forged cookies, malformed signed content, off-site `next`, CSRF, no
  database writes, no reservation or sale created.
* Photo isolation (`tests/test_public_catalog_photos.py`): published served
  with ETag and cache; unpublished, rejected, candidate, hidden-part,
  guessed, traversal and `/media/` paths 404; EXIF removed; invalid content
  and decompression bombs refused.

## Full suite, base against candidate

Same machine, same day, SQLite, `pytest tests/` in clean worktrees.

| | Base `ebd7972` | Candidate `160ab20` |
| --- | ---: | ---: |
| tests | 4,626 | 4,836 |
| failed | 10 | 10 |
| skipped | 118 | 142 |

Failure IDs: 9 shared (the calendar-dependent
`tests/test_clients_overview_sorting.py` set,
`test_partial_repair_line_cancellation.py::test_report_button_confirm_screen_and_redirect_keep_filters`,
`deployment/test_ai_support_renderer.py::test_check_mode_prints_only_redacted_status`).
Fixed by the candidate: `test_part_search.py::test_hits_hydrate_through_stage1_public_facts_in_rank_order`.
One candidate-only failure appeared in that run
(`test_public_catalog_migrations.py::test_upgrade_backfills_unique_public_ids_and_publishes_nothing`,
seed data flushed by an earlier transactional test); it was fixed in
`ccbb9fd` and the final numbers are in the handoff document.

## Reproduce

```
DATABASE_URL=postgres://<owner>@127.0.0.1:<port>/<db> python manage.py migrate
DATABASE_URL=... python manage.py seed_public_catalog_demo --confirm-isolated
psql -d <db> -v public_role=denstock_public -f scripts/operations/create_public_catalog_role.sql
DATABASE_URL=... python manage.py generate_public_catalog_stage2_qualification --confirm-isolated
DATABASE_URL=... python scripts/qualification/public_catalog_launch_benchmark.py \
    --confirm-isolated --expect-database <db> --enrich --output evidence.json
python scripts/qualification/public_catalog_acceptance.py --base-url http://127.0.0.1:<port> \
    --article 420892388 --exercise-cart --probe-post
python scripts/qualification/public_catalog_load.py --base-url http://127.0.0.1:<port> --clients 8
DENSTOCK_TEST_DATABASE_URL=postgres://... pytest tests/test_public_catalog_*.py \
    tests/test_public_runtime_boundary.py tests/test_part_search_postgresql.py
```
