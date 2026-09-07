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
