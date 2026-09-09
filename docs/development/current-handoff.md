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

Branch: `codex/next-product-package` at `1c9407e1f2658d8763b5b0d348fc2b376e331ba7`.

The production decision is confirmed: every newly completed Sale or Repair,
through every entry point, requires remembered gross weight, net weight and
application area. Quick Actions, cart and `perform_action` already reject
missing data. Independent review tests H1-H6/S8-1/S9-1 pass; PG16 restoration
of fresh production dump and candidate migrations also passed. Production was
not changed.

The remaining real runtime gap is regular completion:

- `apps/sales/views.py:sale_complete` calls `complete_sale` directly;
- `apps/repairs/views.py:repair_order_complete` calls `complete_repair_order`
  directly;
- neither normal detail screen currently collects missing customs metadata;
- shared `complete_sale` / `complete_repair_order` also need a fail-closed
  final invariant.

Implement a shared metadata-completion workflow: inspect locked document
lines, show only parts whose effective remembered data are missing or invalid,
accept whole grams and the approved five application values, persist metadata
and complete atomically, then enforce the same check in the shared completion
services. Preserve legacy read/export resilience. Afterwards migrate successful
test fixtures using `tests.customs_support.remember_customs` narrowly; do not
auto-fill negative H1/H2 or legacy-history tests. Full suite currently has many
expected obsolete-fixture failures from direct Sale/Repair calls without the
new required metadata.
