# Current handoff

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
