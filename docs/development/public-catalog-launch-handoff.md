# Public catalog launch readiness: handoff

For the next agent, the reviewer and the release operator. No secrets here.

## Branches and SHAs

| What | Branch | SHA |
| --- | --- | --- |
| Starting point (Stage 11 preview candidate) | `codex/public-catalog-stage11-read-cart-hardening` | `ebd79727866c126f9b1a9eb41513dc4531f52d3c` |
| Independent launch-readiness stack | `claude/public-catalog-launch-readiness` | `468e3f6ba90c768ca1b77c2c8ed6a030ce3ac886` |
| Request stack integration (Codex, reviewed, not modified) | `codex/public-catalog-request-stack-integration` | `891e6fd1784af03cce72a71af0c53470e4366985` |
| Integrated launch candidate (reviewed, not modified) | `claude/public-catalog-launch-candidate` | `cf493a2fe2daaff62302ee36aa4de0b95b290405` |
| Codex follow-up (reviewed, not modified) | `codex/public-catalog-launch-candidate-remediation` | `74e2e15021c75821782f9eab4e59fc59ef7dfc53` |
| Final remediation, deployed to the preview | `claude/public-catalog-launch-final-remediation` | `55db9321d41ea5d5e9d3f8d717f94fbc0ae37b20` |
| Release integration with production main | `claude/prostor-launch-integration` | the branch head that contains this file |
| Production | `main` | `a5c014621c3689340bfe699216e03bb0b88d7472` |

The launch candidate starts at the exact request-stack SHA, merges the
launch-readiness stack (`2235214`) and fixes the request write in the real
public runtime (`07ddc4d`). Review findings and evidence:
`docs/qualification/public-catalog-launch-readiness.md`, section
"Integrated launch candidate".

The final night review (2026-09-12) reproduced the evidence, fixed five
defects on top of the candidate and the Codex follow-up, and deployed the
result to the preview: `docs/qualification/public-catalog-final-night-review.md`.

The stack sits on the catalog chain that branched from `073ad9b`.
Production `main` was six commits ahead (operator search, reports, sidebar),
with no migrations. `claude/prostor-launch-integration` is that merge, made
for real (four overlapping files, no conflicts), plus the work below. It is
the branch to review for the release; the two SHAs above are its ancestors
and were not modified.

On top of the merge it adds: one canonical Russian phone record with a shared
input mask, the operator section "Заявки клиентов" wired into the sidebar with
a count of new requests, and the customer-price audit
(`docs/operations/customer-price-audit.md`,
`docs/qualification/customer-price-audit-2026-09-12.md`).

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
* Tools: demo seed, coverage report, HTTP acceptance (with an opt-in real
  request), 125k benchmark, bounded load test.
* Request hand-off (`apps/catalog/public_requests.py`): the cart becomes
  one customer request; zero-stock lines are supply inquiries; one token
  per cart content; session-gated success page with an 8-character
  reference; honeypot and per-address limit; one explicit read-write
  transaction on a read-only role; the write guard applies (a freeze pauses
  requests).

## Migration order (over production `main`)

`actions.0013`, `actions.0014`, `catalog.0007`, `catalog.0008`,
`catalog.0009`, `catalog.0010`, `catalog.0011`, `customer_requests.0001` to
`0004`. Only `0008` touches existing rows (one UPDATE). Details and rollback realities:
`docs/operations/public-catalog-release-runbook.md`.

## Evidence

`docs/qualification/public-catalog-launch-readiness.md` holds the numbers:
PG16 fresh (120 migrations, no drift), PG16 upgrade from `ebd7972` with
fixtures and from a real-data copy of production (fingerprints identical,
no public photo created), query counts flat at 1/20/50, the 125k regression
split into search, service and page, load runs with zero errors, the role
and boundary proofs, and the full-suite comparison.

Full suite (SQLite, same machine, same day), integrated candidate against
its immediate base: base `891e6fd` 4,656 collected, 4,528 passed, 10
failed, 118 skipped; candidate `4597375` 4,918 collected, 4,747 passed, 9
failed, 162 skipped. The 9 candidate failures are all present on the base
(calendar-dependent clients-overview set, the partial-repair report button,
the AI renderer check). Candidate-only regressions: 0. Fixed by the
candidate: the stale Stage 2 hydration test. PG16: 572 passed across the
public, request, catalog, search and write-guard modules. (The independent
stack alone, `f13ee91` against `ebd7972`: also 0 candidate-only.)

## Known limitations (deliberate, documented)

* The request-stack `?supply=<public_id>` shortcut form is folded into the
  cart: a zero-stock part goes to the cart as a supply inquiry and is sent
  with the rest. One path, one mental model.
* The request limit is per process and per address (LocMem cache); it
  stops a script, not a botnet. An edge rate limit in Caddy for
  `/request/submit/` is the next step if abuse appears.
* Operators find a request by its 8-character reference in the list; there
  is no search box on the request list yet.
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

The preview runs `ebd7972`. Upgrading it to the candidate applies
`catalog.0010`, `catalog.0011` and `customer_requests.0001` to `0004` (its
`0008` already ran with the old code and is not repeated), then the role
script with `-v public_role=denstock_public_preview`, then a runtime
recreate. Follow
`docs/operations/public-catalog-preview-runbook.md`, section B, and run the
acceptance script afterwards. Nothing was deployed to the preview tonight.

## Production release prerequisites

1. Independent review of this candidate (it rewrites Stage 5-8 views and
   templates, edits the Stage 4 migration backfill, re-homes the request
   hand-off and changes the write guard for `public-catalog` mode).
2. Codex (or the owner of the request stack) agreeing with the changes to
   their stack listed in the qualification document.
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
* Codex's acknowledgement of the request-stack changes (review table in the
  qualification document).
* The privacy policy page and its URL on the form (legal).
* Yandex Webmaster and Google Search Console after the indexing switch.

## Local environment used (disposable)

A `postgres:16` container `denstock-launch-pg16` on 127.0.0.1:55520 with
synthetic databases only (the production snapshot copies were dropped):
`launch_demo`, `launch_fresh`, `launch_upgrade`, `launch_corpus`,
`launch_e2e`, `launch_up_preview`, `launch_up_codex`, `launch_up_prod`,
`launch_codexint`, and throwaway local roles for the rehearsals. Remove it
when no longer needed: `docker rm -f denstock-launch-pg16`.
