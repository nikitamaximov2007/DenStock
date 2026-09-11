# Public catalog launch readiness: handoff

For the next agent, the reviewer and the release operator. No secrets here.

## Branches and SHAs

| What | Branch | SHA |
| --- | --- | --- |
| Starting point (Stage 11 preview candidate) | `codex/public-catalog-stage11-read-cart-hardening` | `ebd79727866c126f9b1a9eb41513dc4531f52d3c` |
| Independent launch-readiness stack | `claude/public-catalog-launch-readiness` | the branch head that contains this file |
| Final launch candidate with the request stack | not created | `origin/codex/public-catalog-request-stack-integration` had not been published |
| Production | `main` | `a5c014621c3689340bfe699216e03bb0b88d7472` |

The stack sits on the catalog chain that branched from `073ad9b`.
Production `main` is six commits ahead (operator search, reports, sidebar),
with no migrations. A trial merge of `origin/main` into this branch merged
without conflicts (throwaway worktree, not committed). The release branch
must include that merge and be re-qualified on the same-day baseline.

## What this stack adds (read the commits for detail)

* One public read service (`apps/catalog/public_catalog.py`): visibility,
  composable filters over the whole ranked list, facets, confirmed
  original/analog filter and labels, cards.
* Launch UI: search-first home, results, part page, cart, error pages; one
  stylesheet, no JavaScript, strict CSP; mobile checked at 320-1280 px.
* Cart with supply-inquiry lines for zero stock, reasons for refusals,
  tamper-proof signed cookie, no database writes.
* Stage 14 photos: explicit publication with provenance, Pillow renditions
  in the database, moderation in the part card and a queue page, public
  serving re-checked per request, RLS for the public role.
* SEO: canonical from `PUBLIC_CATALOG_BASE_URL`, env-driven indexing
  (default noindex), sitemap index, truthful Product JSON-LD, Open Graph.
* Runtime: public settings and middleware shared with tests, access log,
  503 page on database failure, threaded Gunicorn with persistent
  connections, Secure cookies by default, no admin app in the public process.
* Role script: exact read graph, RLS, read-only sessions, statement timeout,
  connection limit.
* Migrations: `catalog.0010` (photo tables, no rows), `catalog.0011`
  (database defaults for an app-only rollback), and a set-based backfill in
  `catalog.0008` (1,903 s to 2 s on the real catalog copy).
* Tools: demo seed, coverage report, HTTP acceptance, 125k benchmark,
  bounded load test.

## Migration order (over production `main`)

`actions.0013`, `actions.0014`, `catalog.0007`, `catalog.0008`,
`catalog.0009`, `catalog.0010`, `catalog.0011`. Only `0008` touches existing
rows (one UPDATE). Details and rollback realities:
`docs/operations/public-catalog-release-runbook.md`.

## Evidence

`docs/qualification/public-catalog-launch-readiness.md` holds the numbers:
PG16 fresh (120 migrations, no drift), PG16 upgrade from `ebd7972` with
fixtures and from a real-data copy of production (fingerprints identical,
no public photo created), query counts flat at 1/20/50, the 125k regression
split into search, service and page, load runs with zero errors, the role
and boundary proofs, and the full-suite comparison.

Full suite (SQLite, same machine, same day): base `ebd7972` 4,626
collected, 4,498 passed, 10 failed, 118 skipped; candidate `f13ee91` 4,845
collected, 4,693 passed, 9 failed, 143 skipped. The 9
candidate failures are all present on the base (calendar-dependent
clients-overview set, the partial-repair report button, the AI renderer
check). Candidate-only regressions: 0. Fixed by the candidate: the stale
Stage 2 hydration test. Catalog suites on PG16: 413 passed.

## Known limitations (deliberate, documented)

* No request submission yet: the cart ends at a summary. The request stack
  (Stages 9, 10, 12, 13) plugs into `{% block cart_actions %}` in
  `templates/public_catalog/cart.html`; map cart `public_id`s to parts
  through `public_parts()` so a request can never name a hidden part.
* Content coverage is low by design at launch (no confirmed Russian names,
  photos or analogs in the 2026-09-06 copy); the pages handle every gap.
  See `docs/operations/public-catalog-launch-data-quality.md`.
* The "Техника" filter stays hidden until results contain an explicit
  application area or compatibility.
* Search 2.0 on real data: article prefix and substring tiers cost about
  34 ms each for short numeric input (`4208`: 70 ms in Search 2.0). Worth a
  separate Search 2.0 tuning task; unchanged here.
* Typo searches over words shared by most of the catalog stay the Stage 2
  adversarial cost (up to about 0.6 s on the synthetic corpus).
* Internal media on the admin host remain reachable without login by their
  unguessable UUID names (pre-existing Layer 25 behaviour, unchanged).
* `config/settings/prod.py` still reads `DJANGO_SECURE_COOKIES` without a
  boolean cast for the internal runtime (pre-existing; the public runtime
  now parses it correctly).

## Preview upgrade

The preview runs `ebd7972`. Upgrading it to this stack applies
`catalog.0010` and `catalog.0011` (its `0008` already ran with the old code
and is not repeated), then the role script with
`-v public_role=denstock_public_preview`, then a runtime recreate. Follow
`docs/operations/public-catalog-preview-runbook.md`, section B, and run the
acceptance script afterwards. Nothing was deployed to the preview tonight.

## Production release prerequisites

1. Independent review of this stack (it rewrites Stage 5-8 views and
   templates and edits the Stage 4 migration backfill).
2. The request stack integrated on top, reviewed, with the request-domain
   role design from the release runbook.
3. Merge of the then-current `origin/main`, full-suite comparison against
   the same-day `main`, PG16 upgrade rehearsal on a fresh production copy.
4. Legal wording for personal-data consent (request stack).
5. Domain and DNS if launching on `pro-stor.ru`
   (`docs/operations/public-catalog-domain-readiness.md`).
6. A release window with PRE and POST signed, offsite-verified backups.

## Rollback

First move: stop `catalog-web` and remove its Caddy host. Application-only
rollback of the internal runtime is safe after the migrations (database
defaults from `0011`). A schema rollback drops photo decisions and analog
confirmations and changes public IDs on re-release. Restore from the PRE
backup only for data damage. Full text in the release runbook.

## Remaining external items

* DNS and TLS for `pro-stor.ru` and `admin.pro-stor.ru`.
* Telegram and MAX credentials for the request stack.
* Legal review of consent and privacy wording.
* The Codex request-stack integration branch.
* Yandex Webmaster and Google Search Console after the indexing switch.

## Local environment used (disposable)

A `postgres:16` container `denstock-launch-pg16` on 127.0.0.1:55520 with
databases `launch_demo`, `launch_fresh`, `launch_upgrade`, `launch_corpus`,
`launch_snapshot` and `launch_snapshot2`. It holds a copy of production
data (the 2026-09-06 snapshot restores) and should be removed when no
longer needed: `docker rm -f denstock-launch-pg16`.
