# ACTIVE HANDOFF: Quantity provenance Round 7

Branch `codex/quantity-provenance-blockers`, baseline
`172d09f0eabf837c417fc5216e6ddade671e27ab`. This candidate remains
unreleased. Do not deploy, merge, or rebase it onto `main` before the final
independent retest.

Round 7 closes two PostgreSQL/Python parity defects reproduced on the baseline:

- Real Django admin edits to a transfer lot note (`Перемещение #без номера`,
  a space after `#`, or Unicode digits) made Python return `UNKNOWN` while SQL
  accepted nearby transfer evidence. The historical and explicit-origin cases
  now require the exact machine-written note, including the ASCII decimal id
  and stored source-location snapshot. A malformed transfer claim cannot fall
  back to another transfer. Two plausible historical creators remain `UNKNOWN`.
- `creation_origin=''` persisted through `QuerySet.update` and made Python
  return `UNKNOWN` while SQL treated it as NULL and returned a supplier receipt.
  Normal ORM updates, bulk updates, and model saves reject invalid origin
  mutation. The receipt service alone performs the checked, one-way
  `supplier_pending` to `supplier_received` transition. Migration 0019 adds a
  CHECK constraint allowing only NULL and the six declared marker values.
  SQL no longer converts empty text to NULL, so pre-constraint corruption
  remains `UNKNOWN` in both readers.

PostgreSQL 16 focused qualification: 308 passed, 1 expected failure for the
separate status-integrity issue below. A fresh production dump was restored
only in an isolated local PostgreSQL 16 database, migrated through 0016,
0017, 0018, and 0019. All 984 old markers and explicit-origin references
remain NULL; the candidate Python classifier and SQL agree on all 984 lots
(785 supplier, 13 transfer, 186 found; zero mismatches). Production remained
at `6916276df5de686c457914ff36e77b3e921dc322`, PostgreSQL 16.14, and
`/healthz/` returned HTTP 200 with database OK at the fresh read-only check.
Full SQLite comparison against `172d09f`: baseline 6544 passed, 9 failed,
316 skipped; candidate 6544 passed, the same 9 failed, 350 skipped.
Candidate-only failures: 0. Ruff, djlint, Django check, migration check, and
diff check passed.

**FOLLOW-UP INTEGRITY TASK REQUIRED:** A completed `StockReturn` can be edited
through the Django admin HTTP change form back to `draft`, then posted a second
time. The second posting writes another `RETURN_LOT` and doubles returned
stock. `tests/test_quantity_provenance_round7.py` documents the reproduction
as a strict expected failure. This is pre-existing and outside the Round 7
parity fix; the return document status transition and repost protection need a
separate integrity change.

---

# Historical handoff: Quantity provenance Round 6.1

Task: close final SQL classifier parity on
`codex/quantity-provenance-blockers`, based on `42ca0e096ba44de1e7131ace443c3f6e585125c1`.
This is an engineering candidate, not a production release. Production and
`main` remain at `6916276df5de686c457914ff36e77b3e921dc322` at the latest
read-only check. The feature branch is not based on current `main`.

Round 6 baseline already completed:

- Reproduced all seven audit findings against the unchanged Round 5 baseline.
- Added a nullable, creation-path marker for new lots; old lots receive no
  fabricated values. Explicit transfer and return links are checked before
  supplier evidence. Contradictions and incomplete return documents yield
  `UNKNOWN`. Supplier receipts and PostgreSQL evidence queries now use the
  same strict document, line, lot and movement checks.
- Blocked Django admin deletion of all StockReturn documents, including
  completed historical returns with no explicit origin link. Posted return
  documents and lines also reject normal ORM deletion; return lines are
  already read-only in the admin inline and have no separate admin endpoint.
  Existing StockTransfer model deletion guards prevent historical MOVE_LOT
  documents from being erased through the ORM. RECEIVE_LOT has no external
  document pointer; its StockLot relation is protected.
- PostgreSQL 16 focused suite: 240 passed. Migration sequence 0016, 0017,
  0018 passed on an isolated production snapshot seeded before migration with
  old receipt, legacy, found, transfer, return, existing-return and same-part
  multiline cases. All old origin columns remained NULL.
- Production HEAD, app commit, actual remote main, PostgreSQL, health, counts
  and candidate SQL were checked read-only. The live SQL output matched the
  restored snapshot byte-for-byte. Candidate classifier on that snapshot:
  984 lots; 785 supplier receipts, 13 transfers, 186 found, 0 returns,
  0 `UNKNOWN`, 0 over-received lines. The 137 receipt-capacity differences
  were already present under the Round 5 rule.
- Full SQLite baseline: 6521 passed, 9 failed, 293 skipped. Candidate: 6540
  passed, the same 9 failed, 320 skipped. Candidate-only failures: 0.
  The exact common failure IDs are:
  - `tests/test_clients_overview_sorting.py::test_screen_default_order_is_recent_first`
  - `tests/test_clients_overview_sorting.py::test_broken_sort_parameters_fall_back_to_default`
  - `tests/test_observability_and_price_labels.py::test_a_part_without_a_price_shows_a_dash_not_a_zero`
  - `tests/test_partial_repair_line_cancellation.py::test_report_button_confirm_screen_and_redirect_keep_filters`
  - `tests/test_unified_operator_price.py::test_a_part_without_a_price_shows_a_dash_not_a_zero`
  - `tests/test_zero_price_sale_guard.py::test_search_shows_a_dash_for_a_part_without_a_price`
  - `tests/deployment/test_ai_support_renderer.py::test_check_mode_prints_only_redacted_status`
  - `tests/test_max_bot_compose.py::test_max_bot_mounts_only_the_public_ca_directory_read_only`
  - `tests/test_max_edge_route.py::test_only_the_public_catalog_block_changes`
- Static checks: ruff, djlint, Django check, migration check and diff check
  passed.

Owner decision for Round 6.1 is final: a historical lot without surviving
positive supplier evidence is `UNKNOWN`, even when it may once have been a
legitimate legacy supplier lot. A `supplier_pending` creation marker plus the
unique line and reconstructed ledger is required for `LEGACY_PRIMARY`.

Round 6.1 candidate work:

- Added a complete, read-only `final_provenance` SQL query. It returns one of
  all nine Python classes for every lot and follows the same explicit-origin,
  contradiction, receipt, transfer, pending, recount, found, legacy and UNKNOWN
  ordering. Existing evidence queries remain available for investigation.
- PostgreSQL 16 parity assertions now run after every fixture in the core,
  adversarial and Round 6 regression modules, comparing final classes for all
  fixture lots, including damaged documents and erased evidence.
- A later return posted within one second of a found-stock or recount origin
  exposed a real shared classifier bug: both sides used weak return completion
  timing to displace the first, marked ADJUST_IN origin. Python and SQL now
  preserve the proven found/recount origin; SQLite and PostgreSQL regressions
  cover it.
- A fresh read-only production dump was restored and migrated only in an
  isolated local PostgreSQL 16 database. Python and final SQL agreed on all
  984 lots: 785 `PRIMARY_RECEIPT`, 13 `TRANSFER_DERIVED`, 186 `FOUND_STOCK`;
  zero mismatches. Production itself remains unchanged at `6916276` and
  PostgreSQL 16.14 at the latest read-only check.
- Focused PostgreSQL qualification: 277 passed, including the seven Round 6
  blockers, admin return deletion, AUD-01, delayed and existing-lot returns,
  same-part multiple BatchLines, F1 rebound, quantity and return suites.
- Full SQLite comparison against `42ca0e0`: baseline 6540 passed, 9 failed,
  320 skipped; candidate 6544 passed, the same 9 failed, 321 skipped.
  Candidate-only failures: 0. Ruff, Django check, migration check, djlint and
  diff check pass. No new migration, deploy or merge.

---

# Historical handoff: Customer request to sale final release

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
