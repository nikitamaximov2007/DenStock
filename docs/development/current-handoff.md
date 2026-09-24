# ACTIVE HANDOFF: Oil inventory + revenue/cost/profit final RC (partial - not RC-ready)

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
Current commit: `2fd9533`.

**This handoff is honest about scope: part (B) is done and qualified. Part
(A) has only its data-model foundation done. Do not present this as a
finished oil-inventory RC - it is not one.**

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

**Part (A) - oil data model foundation only:**

- `PartType.is_oil` / `PartType.oil_package_volume_l` (additive migration
  `catalog/0020`), reusing the existing `Decimal(max_digits=12,
  decimal_places=3)` quantity fields project-wide as LITERS for oil rather
  than adding new columns (confirmed via two research passes that
  `StockLot`/`StockMovement`/`SaleLine`/`RepairIssueLine`/
  `InventoryCountLine`/`SectionRecountLine` are already exactly this type,
  so no widening is needed anywhere).
- DB `CheckConstraint` enforcing volume required-iff-oil. **Caught and fixed
  a real bug while writing it**: SQL CHECK constraints treat a NULL
  comparison as passing (three-valued logic), so
  `is_oil=1 AND oil_package_volume_l > 0` let a direct `.update()` bypass
  set `is_oil=True` with a NULL volume straight through the DB constraint
  even though `clean()` would have caught it. Fixed by adding an explicit
  `oil_package_volume_l__isnull=False`. Covered by a regression test using
  a raw `.update()` that bypasses model validation.
- `PartType.clean()` guards blocking `is_oil`/`oil_package_volume_l`
  changes once `PartType.has_stock_or_history()` is true (StockLot,
  StockMovement, PartItem, SaleLine or RepairIssueLine exist for that
  part). Also gives `can_change_tracking_mode()` a real implementation
  (it was previously an always-`True` stub with a stale TODO).
  **Note**: this guard fires as soon as ANY stock lot exists, even before
  a first sale - stricter than "sales or repair history" alone. That was a
  deliberate conservative default, not verified against an owner decision.
- `PartTypeForm` (the full edit form, not the quick-create `ManualPartForm`
  - matches the existing "quick-create is minimal, edit form is complete"
  split) now exposes both fields with the project's comma-decimal input
  convention.
- `tests/test_oil_part_type.py` (16 tests): model constraint, guards, form.

## NOT done (part A) - the bulk of the original oil spec

None of this exists yet. Listed in the rough order a follow-up session
should tackle it, with what the two background research passes already
established as a head start (do not re-research these, act on them):

1. **Sale flow**: "Объём, л" label instead of "Количество, шт." on
   `AddSaleLotForm`/`sale_detail.html` when the selected lot's part is oil;
   price-per-liter suggestion from package price / package volume. No
   service-layer change is needed for money/stock correctness - `quantity`
   and `unit_price` on `SaleLine` already work exactly right for fractional
   liters as-is (confirmed: `add_stock_lot_to_sale`/`complete_sale`/
   `sell_stock_lot` do plain Decimal arithmetic, no truncation). What's
   missing is UI clarity and the immutable snapshot fields listed next.
2. **Historical snapshot fields for oil sale/repair lines**: add
   `oil_package_volume_l_snapshot` and `oil_package_price_rub_snapshot` to
   `SaleLine` (and the repair equivalent below), frozen at line-add/complete
   time like `unit_cost_rub`/`unmarked_unit_price_rub_snapshot` already are.
   Not started - do not add these fields speculatively before the UI that
   populates them exists (see AGENTS.md: no half-finished abstractions).
3. **Repair flow**: same pattern as Sale - "Объём залитого масла, л" label,
   and give `RepairIssueLine` the same `unmarked_*` dealer-base cost
   snapshot mechanism `SaleLine` already has (it currently only has landed
   cost via `_freeze_repair_line_cost`) so Part B's Себестоимость
   definition is consistent for repairs too.
4. **Receiving/counting integer gates** (confirmed real, not hypothetical,
   by a dedicated research pass - see exact line numbers below): most of
   procurement/receiving/stocktaking is already Decimal-clean and needs NO
   changes (`BatchLine`, `finalize_cost`, `create_stock_lot`,
   `receive_stock_lot`, `InventoryCountDocument` flow, `apps/actions/`,
   `apps/core/part_lookup.py` - all confirmed fraction-safe already).
   What genuinely blocks oil:
   - `apps/inventory/models.py` `FoundStockPosting.quantity` is a
     `PositiveIntegerField` (DB-level integer), and
     `apps/inventory/services.py::_positive_integer`/
     `_post_found_stock_group` hard-reject fractional quantities. This flow
     is reachable from the scanner batch-receiving queue
     (`apps/core/receiving_queue.py`) but the single-lot
     `add_found_stock` service function is Decimal-clean and unused by any
     view - worth checking whether that unused function is a cleaner base
     to route oil through instead of changing `FoundStockPosting`.
   - `apps/core/receiving_queue.py::add_candidate`/`update_quantity` are
     "+1 per barcode scan" and `int(raw_quantity)` respectively - does not
     make sense for a fluid; oil needs a manual-entry path, not scan-to-
     increment. `templates/core/receiving.html` quantity inputs are
     hardcoded `step="1"`.
   - `apps/stocktaking/section_recount.py::_record_part` (scanner section
     recount) is the same "+1 per scan" pattern with no BULK/SERIAL branch
     at all - same problem for oil during a recount.
   - `tests/test_scanner_stock_addition.py::test_quantity_must_be_positive_integer`
     encodes the integer-only contract as current product behavior; it
     needs explicit rescoping (not weakening) once oil bypasses/extends
     this flow.
5. **Public catalog display**: already resolves the unit label dynamically
   from `PartType.unit.short_name` (`templates/public_catalog/_card.html`,
   `part_detail.html`) - setting an oil PartType's `unit` to a "л."/"литр"
   `Unit` row is likely sufficient there, no template change confirmed
   needed. Internal operator templates (~15-60 of them, e.g.
   `templates/core/receiving.html`, `templates/stocktaking/
   cell_recount_detail.html`, `templates/procurement/batch_detail.html`)
   hardcode the literal suffix "шт." instead of `part.unit.short_name` and
   will show the wrong unit for oil until fixed - full file list was not
   enumerated, only spot-checked.
6. **CustomerRequest -> Sale oil policy**: NOT investigated in enough depth
   to safely code. This task explicitly said V1 may restrict this (keep it
   package-based) but "must be documented, not silently assumed" - so
   document the actual decision here (or restrict/hide oil PartTypes from
   that flow) before shipping, don't guess.
7. **Oil returns/cancellations policy**: NOT decided. The system does not
   track "opened vs sealed" for ANY bulk part today - that judgment
   (restock_status: quarantine vs available) is already an operator
   decision for every bulk consumable return, not something the software
   enforces. The working hypothesis is that oil needs no new return
   mechanism, only this note to the owner that opened-container judgment
   stays manual like it already is for every other bulk part - but this
   was not confirmed with the owner and nothing was coded either way.
8. Full 48-item test matrix and PG16 qualification: not attempted. Docker
   was not available in this workspace, matching the customs-fix RC before
   it - see that RC's report for the exact external commands, the pattern
   is the same.

## Qualification evidence (for the commits actually on this branch, `2fd9533`)

- Full SQLite baseline (`origin/main` `97fb32b`, via a throwaway
  `git worktree`) and full SQLite candidate (`2fd9533`) both have exactly
  the same 6 failing tests, byte-identical test IDs:
  `test_observability_and_price_labels.py::test_a_part_without_a_price_shows_a_dash_not_a_zero`,
  `test_partial_repair_line_cancellation.py::test_report_button_confirm_screen_and_redirect_keep_filters`,
  `test_unified_operator_price.py::test_a_part_without_a_price_shows_a_dash_not_a_zero`,
  `test_zero_price_sale_guard.py::test_search_shows_a_dash_for_a_part_without_a_price`,
  `test_max_bot_compose.py::test_max_bot_mounts_only_the_public_ca_directory_read_only`,
  `test_max_edge_route.py::test_only_the_public_catalog_block_changes`.
  Candidate-only failures: 0.
- `ruff check .`: passed (0 findings) on the full repo.
- `djlint templates --check`: 12 pre-existing files would be updated,
  identical on baseline and candidate (none of them touched by this
  branch) - `templates/reports/dashboard.html`, the one template this
  branch changed, individually passes djlint clean.
- `python manage.py check`: passed. `makemigrations --check --dry-run`:
  no changes detected. `git diff --check origin/main...HEAD`: clean.
- `sqlmigrate catalog 0020` reviewed: SQLite recreates the table (Django's
  normal way to add a CHECK constraint on SQLite) via a plain
  `INSERT...SELECT` copy, no data loss risk; on PostgreSQL this migration
  would be a lightweight `ADD COLUMN` + `ADD CONSTRAINT`. Not run against
  PG16 - Docker unavailable in this workspace.

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
