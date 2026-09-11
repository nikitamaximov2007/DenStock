# PRO-STOR public catalog: production cutover design and release runbook

Scope: releasing the launch candidate to production and starting the public
catalog on the authoritative DenisStock database. This document is a plan.
Nothing in it has been executed against production. Every command runs only
inside an approved release window, by the release operator, in this order.

## 1. Final architecture

```
customer ──https──> Caddy ── pro-stor.ru ─────────> catalog-web ──┐
                          └─ admin.pro-stor.ru ───> web ─────────┤
                                                                  v
                                                    PostgreSQL 16 `denstock`
                                                    web: owner role (migrations, all writes)
                                                    catalog-web: denstock_public (least privilege)
```

* One authoritative database. The public catalog does not use a snapshot:
  price and availability are read live by the same facades the warehouse
  uses (`PartType.recommended_price`, `available_totals`).
* `catalog-web` connects as `denstock_public`, created and re-derived by
  `scripts/operations/create_public_catalog_role.sql`:
  SELECT on exactly the public read graph, row-level security on the photo
  tables (published rows only), `default_transaction_read_only = on`,
  `statement_timeout = 5s`, `idle_in_transaction_session_timeout = 30s`,
  `CONNECTION LIMIT 20`. No INSERT, UPDATE, DELETE, sequence, schema or
  function privilege. Warehouse, sales, customers, users, costs, suppliers,
  sessions and internal photos are unreadable.
* Migrations stay privileged and separate: only the internal `web`
  entrypoint (owner role) migrates. `docker/public-entrypoint.sh` never
  migrates, never creates users and never collects static files.
* Connection budget: `catalog-web` holds at most workers x threads
  persistent connections (3 x 2 = 6 by default). PostgreSQL's default
  `max_connections` is 100; the internal web holds a few more.
* Blast radius: a runaway public query is cut at 5 s; a public process that
  misbehaves cannot open more than 20 connections; the public runtime
  cannot write, so a public bug cannot damage warehouse data.

### When the request stack (Stages 9, 10, 12, 13) is integrated

Customer requests need exact INSERTs into the request-domain tables. Keep
the read path read-only and give the writes their own narrow role:

* a second role, for example `denstock_public_requests`, with INSERT on
  exactly `customer_requests_customerrequest` and
  `customer_requests_customerrequestline`, USAGE on their two sequences,
  SELECT only where the service reads back (idempotency lookup), and no
  UPDATE or DELETE;
* a second database alias in `config.settings.public` used only by the
  request submission service (`.using("requests")`), so every catalog page
  keeps running under the read-only default;
* if a single role is preferred instead, the integration must remove
  `default_transaction_read_only` from `denstock_public` and grant the same
  narrow INSERT set. Warehouse and business operational writes stay denied
  in either design, and the PostgreSQL role tests must be extended to prove
  it.

## 2. What the release changes in the database

Migrations over production `main` (`a5c0146`), in order:

| Migration | Change | Locking and duration (126k parts) | Reversible |
| --- | --- | --- | --- |
| `actions.0013`, `actions.0014` | trigram index on confirmed Russian names | index build, seconds | yes (drop index) |
| `catalog.0007` | `pg_trgm` extension, trigram indexes on articles and names | index builds, seconds | yes (drop indexes) |
| `catalog.0008` | `PartType.public_id` (unique, NOT NULL) and `is_public` | table lock for the whole migration; see the rehearsal numbers | schema yes, IDs no |
| `catalog.0009` | analog confirmation fields | metadata only, fast | yes (drop columns) |
| `catalog.0010` | public photo tables, constraints, indexes; no rows | new tables, fast | yes (drop tables) |
| `catalog.0011` | PostgreSQL defaults for the new NOT NULL columns | metadata only, fast | yes (drop defaults) |

`catalog.0008` is the only migration that touches existing rows. It assigns
a random public ID to every part in one set-based statement. The internal
`web` is stopped while its entrypoint migrates, so the table lock affects
nobody, but the release window must include the measured duration.

Business data rows are unchanged except the new columns. The migration
counts as business writes for the deployment state: `business_generation`
in the POST backup manifest will be higher than in the PRE manifest, and the
combined `business_sha256` changes because rows gained columns. Prove that
nothing else changed with the per-table markers in the manifest (`tables`):
every table's `sha256` must be identical except `catalog.parttype` and
`catalog.partanalog` (new columns, same `count` and `max_pk`) and the new
`catalog.publicpartphoto` and `catalog.publicpartphotorendition` (empty).

## 3. Release procedure

Placeholders: `<SHA>` is the reviewed candidate, `<PREV>` the SHA currently
deployed (`git -C /opt/denstock rev-parse HEAD`), `<DIR>` a backup folder
name inside `BACKUP_ROOT`.

### PRE

1. Confirm the candidate: exact SHA, independent review done, full suite
   evidence against the same-day `origin/main` baseline, PG16 fresh and
   upgrade evidence.
2. Record `<PREV>` and `docker compose ps`.
3. PRE backup, signed and offsite in one run:
   `/usr/local/sbin/denstock-backup-capped`. Note the folder name `<DIR>`.
4. Verify it: `docker compose exec -T web python manage.py verify_backup <DIR>`
   (the folder name, not the manifest UUID). Record `business_generation`
   and `business_sha256` from the manifest.
5. Confirm the offsite copy of `<DIR>` is listed at the offsite target.
   Do not continue without a verified, offsite PRE backup.

### Deploy the code and migrate (internal runtime)

6. `cd /opt/denstock && git fetch origin && git checkout <SHA>` (detached
   HEAD; `docker-compose.signing.yml` is untracked and survives).
7. Set `DENSTOCK_APP_COMMIT=<SHA>` in `.env`.
8. `docker compose up -d --build --no-deps web`. The entrypoint runs the
   migrations above as the owner. `--no-deps` keeps PostgreSQL untouched.
9. `docker compose logs --tail=200 web` shows the migrations applied and
   Gunicorn started; `docker compose exec -T web python manage.py
   showmigrations | grep '\[ \]'` prints nothing.
10. Internal smoke: login, a part card (the photo moderation block renders),
    the scanner search, one existing report.

### Public role and runtime

11. Apply the role script as the owner, connected to the production
    database:
    `docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
    -v ON_ERROR_STOP=1 -v public_role=denstock_public -f - <
    scripts/operations/create_public_catalog_role.sql`
12. First release only: set the role password interactively
    (`docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"`,
    then `\password denstock_public`) and store it in the secrets store.
    Never put it in Git or shell history.
13. Write `.env.public` (see `public-catalog-domain-readiness.md`), with
    `PUBLIC_CATALOG_INDEXING=false` for the first start.
14. `docker compose --profile public-catalog up -d --build --no-deps catalog-web`
15. `docker compose exec -T catalog-web python -c "import urllib.request;
    print(urllib.request.urlopen('http://localhost:8000/healthz/').read())"`
    returns `{"status": "ok", "db": "ok"}`.

### Edge

16. Caddy host mapping for the public hostname (separately approved change
    of `docker/caddy/Caddyfile` or `CADDY_PUBLIC_CATALOG_HOST`), then
    `docker compose exec proxy caddy reload --config /etc/caddy/Caddyfile`.
    Keep `X-Robots-Tag: noindex, nofollow` until the indexing switch.

### Acceptance

17. From a workstation, read-only:
    `python scripts/qualification/public_catalog_acceptance.py
    --base-url https://<public host> --article 420892388 --expect-indexing off`
    must end with `"failed": 0`.
18. Run `docs/operations/public-catalog-acceptance-checklist.md` (manual
    part: mobile, photos, analogs, cart).
19. Internal `ops_check` still PASS; internal host still serves staff.

### POST

20. POST backup with `denstock-backup-capped`, `verify_backup`, offsite
    listing. Compare the manifests' per-table markers with PRE as described
    in section 2.
21. Record `<SHA>`, backup folders, acceptance JSON and timings in the
    release record.

## 4. Rollback, honestly

Choose by what went wrong.

**Public catalog misbehaves, internal DenisStock is fine.**
`docker compose stop catalog-web` and remove the public host from Caddy
(reload). The internal system keeps running on the new code. Nothing else
changes. This is the default first move.

**Internal DenisStock misbehaves after the release.**
Roll back the application only: `git checkout <PREV>`, set
`DENSTOCK_APP_COMMIT`, `docker compose up -d --build --no-deps web`, stop
`catalog-web`. Keep the database as it is. This works because:

* all new migrations only add columns, tables and indexes;
* `catalog.0011` gives the new NOT NULL columns database defaults, so the
  previous code can still create parts (new random public ID, public) and
  link analogs (unconfirmed);
* the previous code never reads the new columns or tables.

Caveat: the previous code does not know `PublicPartPhoto`. If photos were
published, a hard delete of such a part or internal photo by old code would
fail on the foreign key (Django cascades in Python, not in the database).
Parts and photos are normally deactivated, not deleted; if a hard delete is
needed during the rollback period, reject the photo first or run the reverse
migration below.

**The schema itself must go back.** `docker compose exec -T web python
manage.py migrate catalog 0006` before switching code, then roll the
application back. Realities:

* reversing 0010 drops every photo decision and rendition (the internal
  source photos stay);
* reversing 0009 drops analog confirmations: every analog must be
  re-confirmed after a later re-release;
* reversing 0008 drops `public_id`: a later re-release assigns NEW random
  public IDs, so every shared part link and every indexed URL changes.
  Before the indexing switch this costs little; after it, prefer a forward
  fix.

**Data damage.** Restore the PRE backup with the existing restore runbook
(`docs/operations/restore-runbook.md`). Everything written after the PRE
backup is lost; this is the last resort, not a rollback step.

## 5. After the launch

* Indexing switch: `public-catalog-domain-readiness.md`.
* Coverage: `docker compose exec -T web python manage.py
  public_catalog_coverage_report` (read-only) to plan content work; see
  `public-catalog-launch-data-quality.md`.
* Access log: `docker compose logs catalog-web | grep apps.catalog.public.access`
  gives route, status and latency per request, without query strings.
