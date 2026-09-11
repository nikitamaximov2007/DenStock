# PRO-STOR public catalog: preview refresh and upgrade runbook

The preview shows the public catalog on real catalog data without touching
production. Actual layout on the VPS (checked 2026-09-12):

```
catalog.185-250-44-206.sslip.io ──> production Caddy (denstock-proxy-1)
        │  route defined in /opt/denstock, never changed by a preview upgrade
        v
catalog-web-preview  (compose project denstock-catalog-preview,
        │             networks: its own + denstock_default for Caddy)
        │  role denstock_public_preview
        v
catalog-preview-db   (its own postgres:16 container and volume)
        database denstock_catalog_preview, owner denstock_catalog_owner
```

* Checkout: `/opt/denstock-catalog-preview` (a git clone on a detached
  HEAD). Untracked and preserved across checkouts: `.env.public`, `.env.db`,
  `docker-compose.preview.yml`, `backups/`.
* ALWAYS pass `-f docker-compose.preview.yml`. In that directory the
  default `docker-compose.yml` describes the production-shaped stack.
* The preview database is separate from production's `denstock-db-1`;
  no preview command addresses the production database.

## A. Refresh the preview data from production

1. Pick the newest signed backup and verify it:
   `docker compose exec -T web python manage.py verify_backup <DIR>`.
   Use the dump inside that folder; do not dump production live for this.
2. Check the dump is real: `head -c 5 <dump> | xxd -p` prints `5047444d50`.
3. Recreate the preview database. First prove the target:
   `psql ... -d denstock_catalog_preview -Atc "select current_database()"`
   must print `denstock_catalog_preview`. Then, connected to `postgres`:
   `DROP DATABASE IF EXISTS denstock_catalog_preview;
   CREATE DATABASE denstock_catalog_preview OWNER $OWNER;`
4. Restore: `pg_restore --no-owner --no-acl -d denstock_catalog_preview <dump>`.
   With `docker compose exec`, always pass `-T` for binary streams.
   Verify about 85 or more tables and a plausible `catalog_parttype` count
   before trusting anything.
5. Continue with section B from step 3 (migrations for the candidate).

The restored database contains production personal data (customers,
sales). The preview role cannot read those tables, but treat the preview
database with production-level care and drop it when the preview ends.

## B. Upgrade the preview to a new candidate

Run on the VPS as root, in `/opt/denstock-catalog-preview`. `<SHA>` is the
reviewed commit, `<TS>` a timestamp.

1. No other writer: `ps -eo args | grep -E "docker compose|git |manage.py|pg_dump"`
   shows nothing, `git status --short` is empty, `git reflog -3` shows no
   unexpected recent checkout. If someone else is working, wait.
2. Backup, then prove it restores:
   ```
   docker exec catalog-preview-db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
     > backups/preview-pre-<TS>.dump
   head -c 5 backups/preview-pre-<TS>.dump | xxd -p        # 5047444d50
   sha256sum backups/preview-pre-<TS>.dump
   docker exec catalog-preview-db sh -c 'createdb -U "$POSTGRES_USER" preview_restore_check'
   docker exec -i catalog-preview-db sh -c 'pg_restore -U "$POSTGRES_USER" -d preview_restore_check --no-owner' \
     < backups/preview-pre-<TS>.dump
   # compare table and part counts with the live database, then:
   docker exec catalog-preview-db sh -c 'dropdb -U "$POSTGRES_USER" preview_restore_check'
   ```
   (`docker exec` without `-t` keeps the binary stream intact.)
3. Code: `git fetch origin && git checkout --detach <SHA>`.
4. Configuration (back up each file first, `cp X X.bak-<TS>`):
   `.env.public` has `PUBLIC_CATALOG_BASE_URL=https://catalog.185-250-44-206.sslip.io`
   and no `PUBLIC_CATALOG_INDEXING=true`; `docker-compose.preview.yml` runs
   `gunicorn ... --worker-class gthread --workers 2 --threads 2 --timeout 60`
   (two workers on the one-vCPU VPS shared with production).
5. Build: `docker compose -f docker-compose.preview.yml build catalog-web-preview`.
6. Migrations as the owner, in a one-off container that is forced into
   development mode (the runtime's `.env.public` would otherwise put it in
   public mode):
   ```
   set -a; . ./.env.db; set +a
   docker compose -f docker-compose.preview.yml run --rm --no-deps --entrypoint "" \
     -e DJANGO_SETTINGS_MODULE=config.settings.dev -e DENSTOCK_MODE=development \
     -e DATABASE_URL="postgres://$POSTGRES_USER:$POSTGRES_PASSWORD@catalog-preview-db:5432/$POSTGRES_DB" \
     catalog-web-preview python manage.py migrate --plan
   ```
   Apply with `migrate --noinput` when the plan lists migrations; the plan
   must be empty afterwards.
7. Role, twice (idempotent), connected to the preview database:
   ```
   docker exec -i catalog-preview-db sh -c \
     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v public_role=denstock_public_preview -f -' \
     < scripts/operations/create_public_catalog_role.sql
   ```
   Required after every upgrade: new tables stay unusable until it runs,
   and it replaces any broader grant an earlier script gave.
8. Recreate only the web runtime:
   `docker compose -f docker-compose.preview.yml up -d --no-deps catalog-web-preview`
   and wait for `(healthy)` in `docker ps`.
9. Acceptance from a workstation:
   `python scripts/qualification/public_catalog_acceptance.py
   --base-url https://catalog.185-250-44-206.sslip.io --article 420892388
   --expect-indexing off --probe-post` must end with `"failed": 0`. With
   `--exercise-cart --submit-request --request-name "PREVIEW ACCEPTANCE TEST"
   --request-comment "Automated preview acceptance - safe to delete"` it also
   proves the request write through the preview role; the request lands in
   the preview database only.

## Rollback

* Application only (the schema changes are additive and `catalog.0011`
  gives old code the defaults it needs):
  `git checkout --detach <previous SHA>`, restore the `.bak-<TS>` copies of
  `.env.public` and `docker-compose.preview.yml`, then steps 5 and 8. The
  role keeps its grants; they include everything older code reads.
* Data: stop the web runtime, then
  `docker exec -i catalog-preview-db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < backups/preview-pre-<TS>.dump`,
  re-run step 7, start the web runtime of the matching SHA.

## C. Safety checks worth keeping

* The preview role lives in the preview's own PostgreSQL container; it
  does not exist in production's cluster at all.
* Only one writer at a time: never upgrade or refresh the preview while
  another person or agent may be doing the same.
* The preview has no `/media` mount. Published photos travel inside the
  restored database, so the preview shows exactly what production has
  published and nothing else.
