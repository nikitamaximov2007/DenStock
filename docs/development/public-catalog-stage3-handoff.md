# Public catalog Stage 2 and Stage 3 handoff

Stage 2 final candidate is `8ce029ba91520882f4caf817df7c8950650aa7af9`
on `codex/public-catalog-stage2-final-qualification`. Its PostgreSQL 16
125,000-row qualification is PASS and is recorded in
`docs/qualification/public-catalog-stage2-pg16-final.md`.

Fresh clean reproduction on 2026-09-10 used a detached worktree at that exact
SHA. It passed the mini fixture, representative article/English/Russian
portable search tests and public-domain contract tests (74 passed, 1
PostgreSQL-only skip), plus Ruff, Django check and migration check. The full
PG16 corpus/planner evidence remains the recorded isolated qualification; it
was not rerun in this concise fresh pass.

Stage 3 candidate is `a1454695b41088dc0b150fe7dd5198574cd8aae0` plus the
follow-up host-routing commit on `codex/public-catalog-stage3-runtime-boundary`.
It adds a separate profile-gated `catalog-web`, public settings and URLconf,
no-migration entrypoint, host route, media/key isolation, URL attack tests and
an owner-run SELECT-only role script.

Before independent review or deployment, run this isolated PostgreSQL 16
acceptance with a role password supplied outside Git:

1. Migrate as the privileged owner.
2. Apply `scripts/operations/create_public_catalog_role.sql`, set its password
   securely, and configure `.env.public` with only the documented public
   variables.
3. Prove Search 2.0, `resolve_current_customer_price`, and `available_totals`
   work as `denstock_public`; prove INSERT/UPDATE/DELETE on catalog, inventory,
   reservation, customer, sale and repair tables are rejected.
4. Start `docker compose --profile public-catalog up -d --build`; verify only
   the public hostname reaches `catalog-web`, internal paths/media return 404,
   and no internal secret/mount appears in `catalog-web`.
