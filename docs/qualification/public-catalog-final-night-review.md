# Public catalog: final night review and preview deployment (2026-09-12)

An independent release review of the launch candidate, reproduced from
scratch in a fresh worktree, followed by the preview upgrade. Production was
only observed (read-only). The last section is the compact pack for a later
cross-review.

## SHAs

| What | Branch | SHA |
| --- | --- | --- |
| Reviewed candidate | `claude/public-catalog-launch-candidate` | `cf493a2fe2daaff62302ee36aa4de0b95b290405` |
| Codex follow-up found at the start of the night | `codex/public-catalog-launch-candidate-remediation` | `74e2e15021c75821782f9eab4e59fc59ef7dfc53` |
| Final remediation (this review) | `claude/public-catalog-launch-final-remediation` | branch head |
| Request stack base | `codex/public-catalog-request-stack-integration` | `891e6fd1784af03cce72a71af0c53470e4366985` |
| Preview before tonight | | `ebd79727866c126f9b1a9eb41513dc4531f52d3c` (running image) |
| Production | `main` | `a5c014621c3689340bfe699216e03bb0b88d7472` |

Neither the candidate nor the Codex branch was modified. The remediation
branch starts at `74e2e15` (a descendant of `cf493a2`), so the Codex
follow-up is part of the reviewed history.

## State found on the preview

At 22:48 server time Codex had checked out `74e2e15` in
`/opt/denstock-catalog-preview`, taken two dumps, built an image and
applied `catalog.0010`, `catalog.0011` and `customer_requests.0001` to
`0004` to the preview database. The role script had not run (the preview
role still had the Stage 11 grants) and the web container had not been
recreated (it still served the `ebd7972` build). Nobody was active when the
review started (no processes, sessions or file changes for two hours).

## Findings and resolutions

| # | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| 1 | High | `74e2e15` granted the public role SELECT on `django_migrations`, stating that Django reads it at startup. As a real LOGIN role without that grant: `manage.py check`, `check --database default`, the entrypoint and every page work; only the operator command `migrate --check` reads the ledger. | `0e6b9af`: grant removed; tests refuse the ledger and run `check --database default` under the role |
| 2 | High | Internal "Заявки клиентов": any refused status change (a stale page offering "Отменить" on a completed request) answered HTTP 500 (`UnboundLocalError` in `customer_request_status`, request-stack code). | `defff8d`: redirect by the URL's pk with the reason; 404 for an unknown request |
| 3 | Medium | Concurrent duplicate clicks carry the same cookie; once the per-address limit was reached the losing click answered 429 although its request existed. | `3d7762a`: a submission whose key already has a request passes the limit |
| 4 | Medium | A line that ran out of stock between the form and sending was sent silently as a supply inquiry (the token covered parts and quantities only). | `ff0e4d1`: line states are part of the fingerprint; the customer confirms again |
| 5 | Low | Request form errors were page-level only. | `5a2ec88`: the field at fault is `aria-invalid` and points at the message |
| 6 | Low | The public form showed a developer note about pending legal review. | removed by `74e2e15` (accepted) |
| 7 | Docs | The preview runbook assumed a shared database; the acceptance checklist placed filters beside results at 768 px. | `35d5ccc` |
| 8 | Product | One Telegram chat can be linked to one request only; a returning customer's second request cannot be linked to the same chat. | recorded, `public-catalog-messenger-runbook.md` |

The earlier findings (the request 500 in the real runtime, readable PII,
the zero-stock rejection, visibility, abuse control, filters, analogs,
paging, canonical URLs, sitemap, migration time, workers, grants) were all
re-verified below.

## Evidence

Isolated PostgreSQL 16.15 in Docker on the workstation; public runtime
through the real entrypoint and Gunicorn gthread 3 x 2 with a LOGIN role
configured by the role script (read-only session default, write guard on,
`DENSTOCK_MODE=public-catalog`); internal runtime on the same database.

**Request runtime (P0).** Cart with an in-stock and a zero-stock part, form,
submit: 302 to the success page, reference shown, one request with two
lines (`420892388` normal at 30,047.00; `PX-01-1503` supply inquiry). No
"permission denied", traceback or 5xx in the runtime log.

**PII.** Column matrix for every request-domain table: the role can SELECT
`customerrequest.id`, `public_id`, `submission_key_hash`, `customerrequestline.id`
and the three deployment-state columns; everything else, including name,
phone, comment, consent fields, messenger contacts, link tokens, status and
privacy events, is refused. Direct login attempts at those columns: all
"permission denied".

**Business rules.** Price 10,000 in the cart, 11,000 at sending: `price_seen`
11,000; posted `price`, `price_seen`, `total`, `part_id` ignored. Cart 3,
available 2 after another reservation: sending blocked, the cart explains,
the form redirects to the cart. Hidden and retired parts: not in search, the
sitemap or cart; detail, photos and cart add 404; a part hidden after it
went into the cart cannot be requested. Idempotency: double click, retry and
changed payload return the same request unchanged; 8 simultaneous clicks, 5
rounds: exactly one request per round, every click on the same page.
Before and after: only the request tables and `business_generation` changed
(reservations, lots, part items, balances, movements, sales, repairs,
customers, ordered parts, prices, analogs and photos identical).

**Operator and messengers.** List and card show the reference; new, in
progress, completed; the refused transition returns with the reason. The
Telegram link is issued; webhook: wrong or missing secret 404, bad JSON 400,
wrong token and replay and expired token not accepted, the valid start
accepted once. A MAX request is stored without credentials and no link is
pretended.

**Photos.** A 4000 x 3000 JPEG with camera maker and GPS EXIF published: card
480 x 360 and detail 1200 x 900 JPEG, no EXIF, `image/jpeg`, nosniff, ETag,
one-day cache. Rejected, candidate, guessed, wrong-variant and traversal
URLs 404; `/media/` 404. As the public login: only published rows are
visible; a photo forced out of "published" with its renditions left in place
shows 0 renditions to the role and 404 over HTTP.

**Analogs and filters.** Confirmed links in both directions, the unconfirmed
link nowhere. Manufacturer, in-stock and both combined match the database
ground truth across all pages (12, 15, 3); 61 results on 4 pages without
duplicates; the application filter follows explicit areas. Paging with
filters is pinned by the search tests.

**Search and SEO.** Exact, normalized, prefix and substring article; exact
and fuzzy English; confirmed and fuzzy confirmed Russian: the expected part
first. An unconfirmed Russian name is neither found nor shown. Canonical
absolute, titles unique, one H1, brand when known, no offer without a price,
OutOfStock at zero stock, noindex, sitemap index, `?page=abc` 200.

**Mobile and accessibility.** Home, results with open filters, the longest
part name, cart and request form at 320, 375, 390, 768 and 1280 px: no
horizontal overflow, one H1, skip link, every control labelled.

**Database role.** Idempotent on a clean database. 32 forbidden statements
(parts, numbers, customs, lots, items, reservations, customers, sales,
repairs, payments acknowledgements, analog and photo moderation, catalog
master data, requests, the deployment write state, DDL) refused by
privileges inside an explicit read-write transaction; the only permitted
update is the write-generation counter. Session: read-only default, 5 s
statement timeout (a 6 s query is cancelled), 30 s idle-in-transaction,
connection limit 20.

**Migrations.** Fresh: 125 migrations in 5 s, nothing pending, no model
drift.

| From | Applied | Time | Fingerprints |
| --- | --- | ---: | --- |
| `ebd7972` | 6 | 0.97 s | 12 identical, no photo created |
| `891e6fd` with requests | 2 | 0.67 s | 15 identical, requests and their PII intact |
| `a5c0146` (production `main`) | 11 | 1.10 s | 11 identical, 31 distinct public IDs |

A role configured by the request-stack script reads a prior customer's name;
after the final script it gets "permission denied" and the runtime still
accepts a new request (acceptance 70/70).

**Query counts (1 / 20 / 50, SQLite | PG16).** Search view 23 | 29 flat; part
facts 8 flat; primary photos 1 flat; part page with N analogs 22 flat; cart
12 flat; request form 9, 12, 12; request submit 22, 28, 28 | 23, 29, 29.

**125k corpus.** Benchmark medians (Search 2.0 / catalog service / page, ms):
exact 3.2 / 9.7 / 10.9; prefix `4208` 4.4 / 22.8 / 17.4; exact EN
5.4 / 20.9 / 25.1; `bearng` 147.6 / 199.7 / 202.8; `проклатка`
562.4 / 616.5 / 617.1; pure read. Real HTTP through the restricted role
(median): exact 13 ms, prefix 18 ms, EN 14 ms, `bearng` 216 ms,
`проклатка` 616 ms, filtered page 32 ms, part page 9 ms.

**Load.** Read mix with the request form, 8 clients for 60 s on 125k: 7,970
requests, 132.8 per second, 0 errors, at most 9 role connections, no lock
waits, workers 225 to 240 MB. Controlled submissions: 200 of 200 accepted
(75.7 complete flows per second, median 100 ms); requests +200, lines +200,
reservations, lots and sales unchanged. The workstation has 10 cores; the
VPS has one.

**Tests.**

| | Base `891e6fd` | Candidate `cf493a2` | Final code `ef7f4a0` |
| --- | ---: | ---: | ---: |
| collected | 4,656 | 4,918 | 4,929 |
| passed | 4,528 | 4,747 | 4,757 |
| failed | 10 | 9 | 9 |
| skipped | 118 | 162 | 163 |

Failures only on the final code: 0. The 9 are the base set without the stale
Stage 2 hydration test (7 calendar-dependent client sorting tests, the
partial-repair report button, the AI renderer check). Focused catalog,
request, search and guard modules (31): PG16 586 passed; SQLite 513 passed,
48 PostgreSQL-only skipped. Static: ruff, `check`, `makemigrations --check`,
`pip check`, `git diff --check`, djlint on 21 changed templates: clean.

## Preview deployment

1. Concurrency check clean; production baseline recorded (SHA, compose,
   `ops_check`, 115 migrations, 88 tables, 902 columns).
2. Backup `backups/preview-pre-night-20260912-004243.dump`: 19,759,298 bytes,
   PGDMP, sha256 `fee613a5a5d4c9fd9b15403d34c9c0f97a0f6e2aec831a477c5c6732cdf94d19`,
   96 table-data entries; restored into a scratch database with identical
   counts (96 tables, 125,987 parts, 126 migrations, 97 sales, 30
   customers), scratch database dropped.
3. Checkout of the reviewed SHA (the clone fetches one branch, so the
   remediation branch was fetched explicitly); config backups
   `*.bak-20260912-004353`; gthread 2 x 2 workers and
   `PUBLIC_CATALOG_BASE_URL`; indexing stays off.
4. Build; migration plan as the owner: nothing to apply; the preview schema
   of every request, photo and identity object equals a fresh migration of
   the final code (162 definitions).
5. Role script twice; resulting grants equal the reviewed set; no ledger.
6. `catalog-web-preview` recreated alone, healthy after about 20 s; the
   preview database container untouched. Business markers identical before
   and after the deployment.
7. Public URL, no login: acceptance 93/93 (TLS, headers, noindex, internal
   paths absent for GET and POST, search variants, detail SEO, sitemap,
   errors). One synthetic request ("PREVIEW ACCEPTANCE TEST",
   `+7 000 000-00-00`, "Automated preview acceptance - safe to delete"):
   reference `C0BD8A7A`, retry on the same request, cart emptied; stored with
   `price_seen` equal to the current price and the draft consent versions;
   the part has no stock on the preview, so the line is a supply inquiry.
   Changes: requests +1, lines +1, write generation +2; everything else
   identical.
8. Resources: 1 vCPU, load 0.25, 858 MB RAM available, 15 GB disk free;
   preview web 120 MB, preview database 236 MB; 3 role connections of a
   limit of 20. Server-side timings on real data: search 73 ms average,
   part page 23 ms, request submit 23 ms.
9. Production after: same SHA, containers not restarted, `ops_check` OK,
   migration ledger and schema identical, write generation unchanged.

Rollback: `git checkout --detach <previous SHA>`, restore the two
`.bak-20260912-004353` files, build (the previous images were not retained),
recreate; data from the verified dump if needed (preview runbook, Rollback).

## Pack for the next cross-review

* Read first: this file, then `public-catalog-launch-readiness.md` (the
  candidate's own evidence).
* Critical migrations: `catalog.0008` (set-based public ID backfill),
  `catalog.0010` (photo tables, schema only), `catalog.0011` (database
  defaults for an application-only rollback), `customer_requests.0001` to
  `0004`.
* Critical files: `scripts/operations/create_public_catalog_role.sql`,
  `apps/catalog/public_requests.py`, `apps/operations/write_guard.py`
  (`public-catalog` mode), `apps/customer_requests/services.py`,
  `apps/catalog/public_photos.py`.
* Critical tests: `tests/test_public_catalog_role_postgresql.py` (grants,
  refused statements, the request under the role, the read-only session
  with the guard on, checks without the ledger),
  `tests/test_public_catalog_requests.py`, `tests/test_customer_requests.py`,
  `tests/test_public_catalog_photos.py`.
* Commands:

```
git diff 891e6fd1784af03cce72a71af0c53470e4366985 <final SHA>
git log --oneline cf493a2fe2daaff62302ee36aa4de0b95b290405..<final SHA>
pytest tests/
DENSTOCK_TEST_DATABASE_URL=postgres://<owner>@127.0.0.1:<port>/<db> pytest \
    tests/test_public_catalog_*.py tests/test_customer_request*.py \
    tests/test_public_runtime_boundary.py tests/test_redteam_emergency_freeze.py
python scripts/qualification/public_catalog_acceptance.py \
    --base-url https://catalog.185-250-44-206.sslip.io --article 420892388 \
    --expect-indexing off --probe-post
```

* Remaining external items: legal texts and the policy link, DNS and TLS for
  `pro-stor.ru`, Telegram and MAX credentials and the one-chat decision,
  host capacity, a production release window.
