# Production release: separate bearing export

Date: 2026-11-18

## Release identity

- Production code and web image revision: `c99bfda8df1d428811bf52ff72be8d35979e03e5`.
- Previous production revision: `ab4d5a1d1f96cb2994d9493a0a52ca19b64637f9`.
- Updated service: `web` only. Telegram, MAX, catalog-web, PostgreSQL, Caddy,
  and the Telegram egress service were left running on their existing images.
- Web image: `sha256:55c755c96b06d8bb2c7666d11cf80d8fff16e3ac8c2fa8ef5827a6d13f48a7c5`
  (`linux/amd64`). Source archive SHA-256:
  `ff55cd41b4b4b85d6559e2a112627fd65f793827fb5592be51b94aa4d32a0a26`.
- No database migration or warehouse-data change was made.

## Backup and rollback

- PRE run: `2026-11-18_11-39-05`, captured with the previous production revision
  recorded in its manifest.
- `verify_backup` passed: Ed25519 signature (`production-1`), database and
  media checksums, and private-media manifest consistency.
- The four backup files were copied to the existing offsite store and checked
  by read-back: 4 matching files, 0 differences.
- Previous web image ID: `sha256:9143481a91f306975fbb378d2410fd3e962072fcbcd81b3ff602df9b75e6c309`.
  It remains available as `denstock-web:rollback-20261118-c99bfda8`.
- Roll back only web by restoring
  `DENSTOCK_WEB_REVISION=ab4d5a1d1f96cb2994d9493a0a52ca19b64637f9` and running
  `docker compose up -d --no-build --no-deps --wait web`. Do not restore the
  database for this code-only release.

## Production verification

Using the same all-time, unassigned-only filters, the historical analog set
contained 47 rows before the split and now contains 38 analog rows plus 9
manually entered bearing rows. The two sets are disjoint and their union is
the original 47-row set. The original export still contains 136 rows.

The three generated workbooks parsed successfully. Their item/formula counts
were: analogs 38/70, bearings 9/20, originals 136/247. All retained the
template's row style. The report template places the bearing button directly
after the analog button and resolves the new route. No account credentials
were used for browser inspection: the report route correctly redirects an
unauthenticated request to login, so a signed-in desktop/mobile visual review
remains for an operator session.

Post-deploy checks passed: release image/runtime identity, all Compose
application health checks, HTTPS `/healthz/` (200), Django `check`,
`makemigrations --check`, and `ops_check`. The web container had zero restarts;
the kernel reported no OOM events during rollout. PostgreSQL, Telegram, MAX,
catalog-web, and Caddy remained healthy/running, and the Telegram reverse-egress
service remained active.

Gunicorn emitted one startup `Control server error` because the configured
non-root home `/nonexistent` is not writable. This is outside the release diff
and does not affect web health or requests; the web container is healthy with
zero restarts and HTTPS health returns 200. No HTTP 5xx or traceback was found
in the new web container's logs. The unchanged pinned Gunicorn version is
26.0.0 in both the previous and released images.

## Qualification and repository state

- Targeted customs/export tests: 93 passed.
- Full candidate suite: 6723 passed, 4 failed, 355 skipped. The exact
  production baseline had 6721 passed, the same 4 failures, and 355 skipped;
  there were no candidate-only failures. The four inherited failures concern
  zero-price labels (three tests) and a partial-repair filter expectation (one
  test).
- Ruff, djlint, Django `check`, migration check, and diff check passed.
- The candidate branch was pushed. `main` remains at the production baseline:
  it was not advanced because the repository's final merge gate requires a
  fully passing pytest run, and the four inherited failures remain.

The eight previously identified title/article discrepancies and the single
ambiguous generic bearing card were left unchanged.
