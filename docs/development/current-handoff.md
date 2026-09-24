# ACTIVE HANDOFF: Oil inventory + revenue/cost/profit final RC (qualified, SQLite-only)

Task: (A) first-class oil (масло) support - fractional-liter tracking
through stock, sale, repair, counting, history, search and public catalog;
(B) fix "Выручка/Себестоимость/Прибыль" so the invariant
`Прибыль = Выручка - Себестоимость` holds for every known-cost scope,
confirm the historical 105 ₽/USD rate stays valid, add two read-only audit
commands, and qualify the result. Full spec is long; see the task history
in this branch's session for the verbatim requirements if picking this up
cold.

Branch: `claude/oil-inventory-profit-final`, based on `origin/main` at
`97fb32b` (built fresh, NOT stacked on `claude/customs-export-manufacturer-fix-mymfob`).
Current commit: `e9ad6e6` (17 commits since base, 61 files, +3387/-114).

**Update: both parts are now implemented end to end and qualified on
SQLite - full baseline-vs-candidate run, 0 candidate-only failures. PG16
was NOT run (Docker unavailable in this workspace) - see "PG16" below for
the exact external commands. A small number of deliberately-scoped V1
decisions remain owner-reviewable, listed under "Owner decisions" below;
none of them block using the branch, they just narrow what it does.**

## Completed and qualified

**Part (B) - revenue/cost/profit fix (the concretely-scoped, high-trust-impact
piece; considered done):**

- `apps/reports/services.py::get_sales_report` rewritten: revenue, cost and
  profit are now computed in one pass over the same completed `SaleLine`
  rows, using `unmarked_unit_price_rub_snapshot` (dealer/wholesale base,
  frozen at sale time) as the ONLY cost basis - landed cost
  (`Sale.revenue_total/cost_total`, frozen and never return-adjusted) is no
  longer used for this report. A line with no confirmed base stays in
  `revenue` but is excluded from `known_revenue`/`cost`/`profit`
  (`profit_unavailable_lines` discloses this - never zeroed, never
  fabricated). Returns are NOT netted into this report (that is
  `get_returns_report`'s job, per the codebase's own pre-existing design
  comment) - all three numbers now share that same non-netted scope, where
  before revenue/cost ignored returns and profit subtracted them. A runtime
  assertion (`profit == revenue - cost` whenever `profit_unavailable_lines
  == 0`) guards the invariant going forward.
- Same fix mirrored in `apps/reports/statistics.py::_movers` (drop the
  return-adjustment there too, for the same scope-consistency reason).
- `apps/reports/exporters.py` and `templates/reports/dashboard.html` labels/
  disclosure text updated to match (disclosure only shown when
  `show_costs`, so it does not leak to storekeeper role - caught and fixed
  a real test failure here).
- **Investigated an already-existing but UNMERGED candidate fix**,
  `origin/codex/sales-report-profit-semantics` commit `f39e15a`. Its
  `get_sales_report` rewrite is structurally the same fix and was a useful
  reference, but its migration `0008_clear_unverified_legacy_base_snapshots`
  NULLs out every `SaleLine.unmarked_*` snapshot whose note starts with
  `legacy_reconstruction_` (the owner-approved 105 ₽/USD historical
  backfill from `sales/0007`). This directly contradicts this task's
  explicit instruction that the 105 rate "must never be invalidated solely
  for being 105/fixed" - so that migration was deliberately NOT adopted.
  Migration `sales/0007` and its backfilled snapshots are untouched here;
  they still feed the report as valid known-cost lines. If anyone later
  merges `f39e15a` on top of this work, migration 0008 must be dropped or
  reworked, not applied as-is.
- New regression test naming the exact cited impossible aggregate
  (409907/368066/112581) in
  `tests/test_sale_price_profit_snapshots.py::test_impossible_aggregate_regression_profit_always_equals_revenue_minus_cost`,
  plus a dedicated legacy-105-still-valid regression test.
- Updated `tests/test_reports.py`, `tests/test_report_exports.py`,
  `tests/test_sale_price_profit_snapshots.py` for the new numbers/labels.
- Docs: `docs/operations/profit-reporting.md` and `docs/ai-support/reports.md`
  rewritten to describe the fixed behavior and the 105-rate status.
- New read-only audits: `manage.py audit_sale_cost_provenance` (classifies
  every completed SaleLine's cost basis as live / legacy_105 / unknown) and
  `manage.py audit_oil_candidates` (lists non-oil PartTypes whose article
  starts with `337`, MOTUL's oil convention, as a human review hint only -
  never auto-classifies).

**Part (A) - oil inventory, now implemented end to end:**

- **Data model & guards**: `PartType.is_oil`/`oil_package_volume_l`
  (`catalog/0020`), DB `CheckConstraint` (with the NULL-bypass fix above),
  `PartType.clean()` immutability guards once `has_stock_or_history()` is
  true, `PartTypeForm` support. `tests/test_oil_part_type.py` (16 tests).
- **Shared oil infrastructure** (so no formula/unit-check is duplicated
  per app): `apps/catalog/quantity_units.py` (single "шт. vs л" decision +
  `quantity_with_unit`/`part_quantity_unit` template filters) and
  `apps/inventory/pricing.py` (`oil_price_per_liter_rub`/
  `oil_line_amount_rub` - package price ÷ package volume, rounded to money
  exactly once to avoid double-rounding drift; `oil_availability_rows` for
  the package/price/available-liters context shown on every screen).
- **Sale**: `apps/sales/services.py::add_oil_volume_to_sale` (operator
  supplies only a volume; price is derived, never typed), a dedicated
  "Масло" section on `sale_detail.html` (`AddOilSaleLotForm`,
  `sale_add_oil_lot` view), `SaleLine.oil_package_volume_l_snapshot`/
  `oil_package_price_rub_snapshot` (`sales/0008`, frozen at add-time,
  idempotently re-derives `total_price` at completion from the same
  snapshot - never from `unit_price × quantity`, which would drift on a
  package that doesn't divide evenly). `_freeze_line_unmarked_price`
  converts the resolved dealer base to per-liter for oil, so the
  already-qualified `get_sales_report()` needed zero changes.
- **Repair**: exact mirror - `add_oil_volume_to_repair_order`, "Масло"
  section on `repair_order_detail.html`, `RepairIssueLine`
  `oil_package_volume_l_snapshot`/`oil_package_price_rub_snapshot`/
  `oil_customer_amount_rub_snapshot` (`repairs/0006` - Repair has no
  persisted total field, so the exact amount itself is frozen, and
  `repair_customer_line_amounts` uses it directly instead of
  re-multiplying a rounded per-liter price).
- **Returns/cancellations**: `apps/returns/services.py::_add_line` rejects
  a generic return of an oil line outright; `cancellation_allocations`
  excludes oil lines from full-document-cancel stock restoration (a
  poured/measured liter is not physically recoverable) while still
  restoring every non-oil line normally; the cancel confirmation screens
  show which oil lines won't be restored
  (`oil_lines_excluded_from_cancellation`).
- **Receiving/counting integer gates** (confirmed real by research, now
  fixed): the found-stock batch queue (`_post_found_stock_group`) and its
  scanner (`receiving_queue.add_candidate`) refuse oil outright rather
  than silently reading a scan count as liters; section recount
  (`_record_part`) still identifies an oil part but creates its line at
  0 L instead of auto-incrementing, so `set_section_line_quantity`
  (already 0.001-precision) is the only way its volume gets recorded.
  `InventoryCountDocument` needed no changes (already Decimal-clean) -
  only its labels now show "л". The quick-action scanner
  (`_perform_action_atomic`) and its cart (`apps/actions/cart.py`) also
  refuse oil - both price from `recommended_price` treated as per-unit,
  which for oil is the PACKAGE price, and neither has a volume input.
- **Display**: internal `part_detail.html`/`scan.html`/recount templates
  show package volume, derived price per liter and available liters via
  `oil_availability_rows`; the public catalog shows liters on the
  availability line while keeping the price line "за упаковку" (package
  price stays the sole public price authority, never reinterpreted as
  per-liter) - `PublicPartFacts` gained `is_oil`/`oil_package_volume_l`.
- **CustomerRequest -> Sale**: V1 policy decided and implemented (not left
  ambiguous) - a public request's `quantity_requested` is packages, so
  `_add_request_stock_lines` skips oil lines rather than reading that
  count as liters; `_validate_request_sale_lines` and
  `complete_request_sale`'s re-pricing loop both know about the skip
  (oil part types are checked for presence, not exact quantity, and
  their already-correct frozen price is never overwritten). The operator
  adds oil to the prepared draft manually via the Sale detail's "Масло"
  section.
- **Audits**: `audit_oil_candidates` extended with `candidate_status`
  (`safe_to_mark` vs `needs_owner_review`, from `has_stock_or_history()`)
  and a second report, `audit_oil_migration_readiness`, covering every
  already-`is_oil` PartType's configuration and real usage counts.
- **Docs**: `docs/ai-support/sales-and-reservations.md` and
  `returns-repairs-writeoffs.md` updated with the oil flow and the
  return-refusal policy and its reasoning.
- Search/barcode: verified unchanged and correct (no code needed) -
  `resolve_part_lookup` never encodes a unit or quantity assumption into
  identity resolution; covered by a direct regression test.

Tests: ~110 new oil-specific tests across `test_oil_part_type.py`,
`test_oil_sale_and_repair.py`, `test_oil_receiving_and_recount.py`,
`test_cost_provenance_and_oil_candidate_audits.py`,
`test_public_catalog_pages.py`, `test_customer_request_sale_flow.py`, plus
one existing test (`test_public_catalog_domain_contracts.py`) updated for
the two new public-safe `PublicPartFacts` fields (caught by the final
full-suite run, not by inline testing - see "Qualification evidence").

## Owner decisions still open (do not block using the branch)

These are deliberate, documented V1 scoping choices, not unfinished work.
Each has a safe default already implemented; revisiting any of them is a
product decision, not a bug fix:

1. **PartType.has_stock_or_history() fires on ANY stock lot**, even before
   a first sale - stricter than "sales/repair history alone" would be. If
   the owner wants oil-field edits allowed while a part has only a
   just-received, never-sold lot, that's a narrower guard to write.
2. **Section recount's zero-quantity first line for oil** is a UX
   compromise (see "Receiving/counting" above) - it still requires a
   manual step per cell, unlike scan-and-go for normal parts. An
   oil-specific "type the volume right after the first scan" prompt would
   be a nicer UX if the owner wants to invest in it.
3. **CustomerRequest oil packages are never auto-converted to a sale
   line** - the operator always adds oil manually after preparing the
   draft. If the owner later wants the public UI to collect a liters
   figure directly, that's a public-form change layered on top of the
   skip logic already in place, not a rewrite of it.
4. **Package-count-based receiving UX** ("Количество упаковок: 3 → 12 л")
   from the task's preferred mockup was not built as a dedicated form;
   the standard Batch/BatchLine receiving flow already accepts a direct
   liter total (explicitly permitted by the task as an alternative), which
   is what the test suite exercises. A package-count convenience form
   could be added later without touching anything else.

## PG16

Not run - Docker daemon unavailable in this workspace (`docker` binary
present, `/var/run/docker.sock` not reachable), same constraint as the
prior customs-fix RC. Exact external qualification, once Docker/a
DATABASE_URL is available:

```
DENSTOCK_TEST_DATABASE_URL=postgres://... uv run pytest -q --maxfail=0
```

Priority areas per the task: `Decimal(12,3)` precision round-trips through
Postgres numeric columns, row locks under concurrent oil Sale/Repair
completion (`select_for_update` on `StockLot` in
`add_oil_volume_to_sale`/`_to_repair_order` and `complete_sale`/
`complete_repair_order` - same locking pattern already proven for normal
parts, not new code), inventory adjustment, the three new migrations
(`catalog/0020`, `sales/0008`, `repairs/0006` - all plain `ADD COLUMN`/
`ADD CONSTRAINT`, reviewed via `sqlmigrate` below), and Decimal report
aggregation in `get_sales_report`. Required: 0 relevant failures.

## Qualification evidence (SQLite, commit `e9ad6e6`)

- Full baseline (`origin/main` `97fb32b`, via a throwaway `git worktree`)
  and full candidate (`e9ad6e6`) both have exactly the same 6 failing
  tests, byte-identical test IDs:
  `test_observability_and_price_labels.py::test_a_part_without_a_price_shows_a_dash_not_a_zero`,
  `test_partial_repair_line_cancellation.py::test_report_button_confirm_screen_and_redirect_keep_filters`,
  `test_unified_operator_price.py::test_a_part_without_a_price_shows_a_dash_not_a_zero`,
  `test_zero_price_sale_guard.py::test_search_shows_a_dash_for_a_part_without_a_price`,
  `test_max_bot_compose.py::test_max_bot_mounts_only_the_public_ca_directory_read_only`,
  `test_max_edge_route.py::test_only_the_public_catalog_block_changes`.
  Baseline: 5940 passed, 229 skipped, 6 failed. Candidate: 6006 passed, 229
  skipped, 6 failed. Candidate-only failures: 0.
- An intermediate full run caught one real candidate-only regression -
  `test_public_catalog_domain_contracts.py::test_public_part_facts_expose_no_internal_stock_or_commercial_fields`,
  a closed-set field guard that correctly flagged the new
  `is_oil`/`oil_package_volume_l` fields on `PublicPartFacts`. Fixed by
  adding both to the test's allowlist (they're already shown on the
  public page itself, not internal data) - the re-run above is the
  result after that fix, not before it.
- `ruff check .`: passed (0 findings) on the full repo.
- `djlint templates --check`: 12 pre-existing files would be updated,
  identical on baseline and candidate; all 11 templates this branch
  touched individually pass djlint clean.
- `python manage.py check`: passed. `makemigrations --check --dry-run`:
  no changes detected. `git diff --check 97fb32b...HEAD`: clean.
- `sqlmigrate` reviewed for all three new migrations
  (`catalog/0020`, `sales/0008`, `repairs/0006`): SQLite recreates the
  `catalog_parttype` table (Django's normal way to add a CHECK constraint
  on SQLite) via a plain `INSERT...SELECT` copy, no data loss risk;
  `sales/0008`/`repairs/0006` are plain nullable `ADD COLUMN`. On
  PostgreSQL all three would be lightweight `ADD COLUMN`/`ADD CONSTRAINT`.
  Not run against real PG16 - see "PG16" above.

## Do not touch

- `apps/sales/migrations/0007_saleline_unmarked_price_snapshot.py` and the
  `legacy_reconstruction_105`-noted `SaleLine` snapshots it backfilled -
  they are valid history per this task's explicit instruction, not a bug.
- Do not adopt `origin/codex/sales-report-profit-semantics` commit
  `f39e15a`'s migration `0008_clear_unverified_legacy_base_snapshots`
  without an explicit fresh owner decision to invalidate that history -
  see the Part (B) section above for why it was rejected here.
- Do not auto-mark any PartType `is_oil=True` from the `audit_oil_candidates`
  output or the "337" prefix - it is a human-review hint only, by design.
- Do not weaken `PartType.has_stock_or_history()` / the oil-field
  immutability guards to "unblock" a migration or a bulk edit - if a real
  need to relax them comes up, that is an owner decision, not a
  workaround.

---

# ACTIVE HANDOFF: Customer request to sale final release

Task: final review and production release of the safe
`CustomerRequest -> Customer -> DRAFT Sale -> completed Sale` workflow, stopping
before any real production sale is finalized.

Branch: `codex/request-to-sale-customer-match-latest`
Candidate: `101d7b936a845df07925ffaa701f2e9b2eba359b`
Qualified base: `212786bc6059c762f953a77d8f6bf9f74354f365`

Completed:

- Refetched actual remote `origin/main`; it had advanced from `95dc50c` to
  `212786bc` with the Telegram keyboard fix.
- Created a fresh branch from `212786bc` and cherry-picked the coherent
  request-to-sale RC without conflicts.
- Added the explicit DRAFT exclusion regression for completed reports and
  customer history projections.
- Pushed the candidate to
  `origin/codex/request-to-sale-customer-match-latest`.
- Verified additive migration semantics, exact normalized-phone matching,
  explicit customer creation, multiple-match fail-closed behavior, current
  price/stock rechecks, atomic finalization, one-to-one request-sale linkage,
  and PostgreSQL locking/idempotency.

Qualification evidence:

- Latest-main affected request/sale and Telegram tests passed.
- Fresh PG16 request/sale/concurrency suite: `58 passed / 0 failed / 0 skipped`.
- Fresh full baseline and candidate on `212786bc` had the same seven inherited
  failures and no candidate-only failures.
- `ruff check .`, `djlint templates --check`, Django `check`,
  `makemigrations --check`, migration plan, and `git diff --check`: passed.
- Public `https://pro-brp.ru/healthz/` read-only check: HTTP 200, `db=ok`.

Not completed / blocker:

- Production SSH/host `/opt/denstock` is unavailable from this workspace:
  `ssh production` cannot resolve the hostname. Production HEAD,
  `DENSTOCK_APP_COMMIT`, active writers, container health, flags/bindings,
  production phone audit, signed PRE/POST backups, deployment, human request
  acceptance, and main alignment were not performed.
- No production database, Customer, Sale, StockMovement, backup, flag, or
  deployment state was mutated.

Exact next steps on the production host, with one writer only:

1. Verify production HEAD, `DENSTOCK_APP_COMMIT`, actual `origin/main`, active
   writers, host health, bindings, and before-counts.
2. Create and verify signed/offsite PRE backup, negative signature control,
   and `rclone check` with zero differences.
3. Deploy exactly the candidate SHA, apply only the additive migration, and
   do not restart PostgreSQL.
4. Run the read-only phone audit and inspect the one real request through
   «Взять в работу» and prepared DRAFT state. Do not press «Провести продажу».
5. Verify unchanged after-counts, create/verify POST backup, then fast-forward
   `origin/main` and verify exact SHA equality.

---

# Historical handoff: Price parity + public phone mask integration

Task: integrate the qualified public-price-parity and public-Russian-mobile-mask
RCs on top of the accepted `origin/main`, qualify the candidate, and release it
only after production preflight, signed backups, live acceptance, and main
alignment.

Branch: `codex/price-parity-phone-mask-integration`
Current commit: `HEAD` on this branch after the handoff update below.

Completed:

- Created the integration branch from accepted `origin/main` `a30ada0`.
- Cherry-picked only the coherent RC commits: price parity `604f3a8` and
  public phone mask `49a60e3`.
- Preserved the accepted Telegram role separation and PartTypeImage to
  PublicPartPhoto pipeline.
- Price resolver now mirrors every finite positive `PartType.recommended_price`
  to PRO-STOR, independent of provenance/certification metadata; unknown or
  non-positive values remain `Уточнить цену`.
- Public requests use strict Russian mobile validation and store compact
  canonical `+79XXXXXXXXX`; internal phone behavior remains unchanged.
- Candidate was pushed to
  `origin/codex/price-parity-phone-mask-integration`.

Qualification evidence:

- Focused SQLite feature/regression tests: passed; 6 browser-dependent tests
  skipped by the local environment.
- Full SQLite baseline `a30ada0`: 5876 passed, 7 failed, 234 skipped.
- Full SQLite candidate: 5902 passed, 7 failed, 234 skipped.
- Candidate-only failures: 0. Failure IDs are identical to baseline.
- PostgreSQL 16 focused qualification: 309 passed, 0 failed, 0 skipped.
- `ruff check .`, `djlint templates --check`, Django `check`,
  `makemigrations --check`, and `git diff --check`: passed.
- Baseline and candidate repo-wide djlint: 0 files would be updated.

Not completed / blocker:

- Production host `/opt/denstock` and its Docker stack are not available from
  this workspace. No verified production writer check, host health check,
  signed PRE backup, live price/phone/Telegram/MAX/photo acceptance, signed
  POST backup, or deployment was performed.
- `origin/main` remains `a30ada0`; it must not be advanced until the candidate
  is deployed and accepted on production.

Exact next steps on the production host, with one writer only:

1. Verify production HEAD, `DENSTOCK_APP_COMMIT`, `origin/main`, active writer
   processes, host health, and bindings.
2. Create and verify the signed/offsite PRE backup with the negative signature
   control and `rclone check`.
3. Deploy exactly the final handoff SHA, apply only pending safe migrations,
   and run health/check/ops checks without restarting PostgreSQL.
4. Run live acceptance for article `517302674`, `audit_public_price_parity`,
   the public phone form, NIKITA ADMIN, DENIS/RIM, and the photo pipeline.
5. Create and verify the signed/offsite POST backup, then fast-forward
   `origin/main` to the deployed SHA and verify SHA equality.

Commands already run: `git fetch origin --prune`, focused and full pytest
baseline/candidate runs, PG16 focused pytest, ruff, djlint, Django check,
makemigrations check, migration plan, diff check, and candidate push.

Commands still required: production runbook preflight, PRE backup, deployment,
live acceptance, POST backup, and fast-forward of `origin/main`.

Known baseline risks: the seven pre-existing full-suite failures are four
zero-price label expectations, one partial-repair filter expectation, and two
deployment/compose expectations. They reproduce unchanged on `a30ada0`.

---

# Historical handoffs

Task: Customs Orders full integration and production release.

Branch: `codex/customs-orders-release`, based on `feature/customs-orders`
candidate `5a749d3`; live is `e5e9bc7` and its actual `origin/main` base is
`c517b6cc9e8fd7b9f52b313ecc2a8eec304a523f` (already an ancestor of this branch).

Completed engineering work:

- Persistent immutable `CustomsOrder` and `CustomsOrderLine` snapshots with
  DB constraints, unique `(source, source_id)` membership and frozen export
  fields.
- Server-side canonical unassigned filter, deterministic boundary order,
  signed displayed selection, atomic revalidation and fail-closed USD/FX
  checks.
- Report membership UI, gray assigned rows, exact order links, bootstrap
  warning and selection preview/modal.
- Frozen two-sheet XLSX and business fingerprint/write-freeze coverage.

Validation completed before snapshot:

- Targeted Customs, XLSX, ordered-parts, UI and navigation tests pass.
- Static checks pass: ruff, djlint, Django check, migration check, pip check
  and diff check.
- Full suite: 4174 passed, 116 skipped, 11 failed. Ten failures reproduce on
  clean `origin/main`: eight `test_clients_overview_sorting`, one partial
  repair cancellation, one macOS root-owned ai-support renderer expectation.
  The remaining navigation expectation was updated for the new approved
  sidebar item and now passes.

Remaining release steps:

1. Review, commit and push the candidate.
2. Take a fresh verified production pg_dump and restore into isolated PG16.
3. Run migration and synthetic #125/#126 acceptance on the snapshot only,
   including fingerprint invariants and XLSX verification.
4. If successful, make signed PRE backup, merge/fast-forward main, deploy,
   run live read-only acceptance, signed POST backup and compare fingerprints.

Never create a test or bootstrap order in production. Production should stay
with zero CustomsOrder rows until the employee creates the real #125.
# H1 regular Sale/Repair completion gate — active handoff

Branch: `codex/next-product-package` at `aa5e383`.

The production decision is confirmed: every newly completed Sale or Repair,
through every entry point, requires remembered gross weight, net weight and
application area. Quick Actions, cart and `perform_action` already reject
missing data. Independent review tests H1-H6/S8-1/S9-1 pass; PG16 restoration
of fresh production dump and candidate migrations also passed. Production was
not changed.

Completed since the last handoff:

- `apps/actions/completion_workflow.py` is the shared regular Sale/Repair
  workflow. It finds only missing or invalid parts, shows their exact article,
  pre-fills remembered kg as integer grams, validates the approved five areas
  and pair of weights, and saves metadata plus a version record.
- Regular completion views render that step, retain entered values on invalid
  POST, re-read document lines in the transaction, then complete atomically.
  Their Back link goes to the actual document, not the POST-only route.
- `complete_sale`, `complete_repair_order`, and direct Quick Actions use the
  same fail-closed service check. Application areas outside the approved five
  are invalid for newly completed documents.
- Tests now cover the regular Sale and Repair UI, missing-only multi-line
  behavior, invalid-pair atomicity and direct-service rejection. H1-H6 and
  S8-1/S9-1 pass, as do Sale/Repair and several migrated fixture modules.
- Candidate commits: `b28bca4`, `e8bef5e`, `13acdc1`, `aa5e383`, all pushed.
- Exact `origin/main` is `8e257c9`; its clean full suite has 10 pre-existing
  failures: eight client-overview sorting tests, one partial-repair cancellation
  test, and the macOS ai-support renderer expectation.

Remaining release-gate work:

1. Continue narrow migration of successful Sale/Repair test fixtures. The
   candidate full suite still has obsolete fixtures that invoke the newly
   guarded services without metadata; do not weaken the production guard or
   globally auto-fill parts. Preserve explicit missing-metadata and legacy
   history cases.
2. Rerun full candidate suite and compare only its remaining failures with the
   actual 10-failure `origin/main` baseline.
3. Rerun H1-H6/S8-1/S9-1, complete static checks, and run the planned synthetic
   acceptance against the already-restored isolated PG16 snapshot. Production
   remains untouched.

# Realtime request workspace — handoff

Branch: `codex/realtime-request-workspace` at `5e8f67f` (based on accepted
`origin/main` `f2e7592`). Production and `origin/main` were not changed.

Completed: durable `WorkspaceEvent` cursor model and authenticated bounded
replay endpoint; transactional request/message/status event emission; locked
sequential human request numbering with deterministic migration backfill;
polling client with reconnect cursor, visibility catch-up, two local sounds,
Enter/Shift+Enter; Telegram-like date separators and customer-name fallback;
central cancelled-request single/bulk deletion with status recheck; removal of
the normal service-information block. Django check, migration check, ruff,
djlint, targeted request tests, and migrations pass.

Added after the initial handoff: validated PNG/JPEG/WEBP/PDF attachment storage,
picker/clipboard UX, Telegram multipart `sendPhoto`/`sendDocument`, and MAX
`/uploads` token-based media delivery. Attachment files are not publicly served
and are removed with their message rows.

Final qualification snapshot: the approved sequential-number copy and
`Отправлено` transport wording are covered by reconciled tests; the fixed
request-write cost is six writes for 1, 20 and 50 lines with no N+1 growth;
and the MAX migration rollback test restores the current `0012` schema so
serialized fixtures remain compatible. PostgreSQL 16 lock qualification
produced eight unique numbers `[1..8]`; focused request/messaging/MAX/
attachment tests passed. Full SQLite candidate qualification is
`5571 passed, 2 failed, 205 skipped`, matching the fresh `origin/main`
baseline exactly; both failures are inherited (`partial_repair` report link
and the unrelated AI-support renderer assertion). Desktop workspace visual
inspection passed on the final HEAD; responsive no-overflow behavior remains
covered by the existing 375px regression tests. The branch is RC-ready and
must not be deployed from this handoff.
