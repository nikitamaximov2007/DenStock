# PRO-STOR public catalog: preview refresh and upgrade runbook

The preview shows the public catalog on real catalog data without touching
production. Current layout (2026-09-11):

```
catalog.185-250-44-206.sslip.io ──> Caddy ──> catalog-web-preview
                                                    │ denstock_public_preview (restricted)
                                                    v
                                         database denstock_catalog_preview
```

The preview database is restored from a verified production backup. The
production database `denstock` is never read or written by the preview
runtime. Every write step below starts by proving which database it is
connected to.

Placeholders: `<PREVIEW_DIR>` is the preview checkout on the server,
`<SHA>` the candidate, `<DIR>` a verified backup folder, `$OWNER` the
PostgreSQL owner role. Run the preview compose project with its own project
name (`-p denstock-preview`) so no command can address production services
by accident.

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

1. `cd <PREVIEW_DIR> && git fetch origin && git checkout <SHA>`
2. Build the preview image: `docker compose -p denstock-preview build catalog-web`
   (or the preview service name used on the server).
3. Migrate the preview database as the owner, bypassing the internal
   entrypoint and pointing explicitly at the preview database:
   ```
   docker compose -p denstock-preview run --rm --no-deps --entrypoint "" \
     -e DATABASE_URL=postgres://$OWNER:<secret>@db:5432/denstock_catalog_preview \
     -e DJANGO_SETTINGS_MODULE=config.settings.prod \
     catalog-web python manage.py migrate --noinput
   ```
   Never run the internal `web` entrypoint for this: it migrates whatever
   `DATABASE_URL` its `.env` holds, which on the server is production.
4. Confirm `showmigrations` (same command, `showmigrations | grep '\[ \]'`)
   prints nothing.
5. Re-derive the grants for the preview role, connected to the preview
   database: `psql -v ON_ERROR_STOP=1 -v public_role=denstock_public_preview
   -d denstock_catalog_preview -f scripts/operations/create_public_catalog_role.sql`.
   This is required after every upgrade: new tables (for example the photo
   tables) are unreadable until the script grants them.
6. Preview `.env.public`: `PUBLIC_DATABASE_URL` with the preview role and
   database, `DJANGO_PUBLIC_ALLOWED_HOSTS=catalog.185-250-44-206.sslip.io`,
   `PUBLIC_CATALOG_INDEXING=false`, `DJANGO_SECURE_COOKIES=true`, a secret
   key that is neither the production internal nor the production public key.
7. Recreate only the preview runtime:
   `docker compose -p denstock-preview up -d --no-deps catalog-web`.
8. Noindex stays on at two levels: the application default and the Caddy
   `X-Robots-Tag: noindex, nofollow` header of the preview host.
9. Acceptance, read-only from a workstation:
   `python scripts/qualification/public_catalog_acceptance.py
   --base-url https://catalog.185-250-44-206.sslip.io --article 420892388
   --expect-indexing off` must end with `"failed": 0`.

## C. Safety checks worth keeping

* The preview role must not be able to open the production database:
  `psql -U denstock_public_preview -d denstock -c "select 1"` should be
  refused. PostgreSQL grants CONNECT to PUBLIC by default, so if both
  databases share one cluster, the owner should revoke it on `denstock`
  (`REVOKE CONNECT ON DATABASE denstock FROM PUBLIC;` after checking which
  roles rely on it). Even when it connects, the preview role has no table
  grants there.
* Only one writer at a time: never upgrade or refresh the preview while
  another person or agent may be doing the same.
* The preview has no `/media` mount. Published photos travel inside the
  restored database, so the preview shows exactly what production has
  published and nothing else.
