# Release image identity

`DENSTOCK_APP_COMMIT` in `.env` is not a release source of truth. Compose uses
`DENSTOCK_BUILD_REVISION` to select the SHA-tagged image and pass the build
revision. The Docker image records that SHA in `DENSTOCK_APP_COMMIT`, the OCI
`org.opencontainers.image.revision` label, and a label containing the SHA-256
of the exact `git archive` source context. These fields contain public hashes,
never secrets.

## Build and verify before an approved release

From a clean checkout at the exact approved SHA, after the release window and
backup gates have passed:

```bash
python3 scripts/operations/release_image_identity.py build \
  --sha <full-40-character-sha> --services web
```

The builder requires HEAD and the release-control files to match the requested
SHA, creates its Docker context from that commit's Git archive, builds only the
named services, then checks the immutable image ID, OCI revision, source
archive digest, and image environment. It fails closed if any value differs.
Its output omits environment contents and build logs. The temporary source
archive is removed when the command exits.

After explicit deployment approval, preserve the existing image IDs, update
the selector, and run the image/config identity gate **before** starting any
service:

```bash
sed -i '/^DENSTOCK_APP_COMMIT=/d' .env
sed -i '/^DENSTOCK_BUILD_REVISION=/d' .env
printf 'DENSTOCK_BUILD_REVISION=%s\n' '<full-40-character-sha>' >> .env
python3 scripts/operations/release_image_identity.py verify \
  --sha <full-40-character-sha> --services web
docker compose up -d --no-build --no-deps --wait web
python3 scripts/operations/release_image_identity.py verify \
  --sha <full-40-character-sha> --services web --runtime
```

For multiple services, pass the same comma-separated list to build and verify.
The configuration gate requires the old `.env` override to be absent and the
image selector to equal the requested SHA. Runtime verification then checks
the actual container image ID and `DENSTOCK_APP_COMMIT`; it never prints other
environment values. The Compose service explicitly maps
`DENSTOCK_APP_COMMIT` from `DENSTOCK_BUILD_REVISION`, which overrides Docker's
image defaults and all `env_file` entries. Thus a stale `DENSTOCK_APP_COMMIT`
cannot silently substitute an older application identity. Keep the `.env`
edits within the separately approved deployment only.

An ad-hoc Compose build without the release tool is labelled `unverified` and
must not pass the release identity check. Do not put credentials, tokens,
environment dumps or customer data in Docker build arguments or labels.

## Compose overlays and services

Read-only inspection on 2026-10-20 found the active production
`COMPOSE_FILE` to contain `docker-compose.yml`,
`deploy/ai-support/docker-compose.external.yml`, and
`docker-compose.signing.yml`. The external overlay adds web UID/GID build
arguments, runtime user/group, and AI bind mounts. The signing overlay adds
the signing-key mount to web and points proxy at the production-managed Caddyfile.
Neither overlay sets `image`,
`build.context`, `build.dockerfile`, `DENSTOCK_BUILD_REVISION`, or
`DENSTOCK_APP_COMMIT`. Compose map merging therefore retains the candidate's
SHA-tagged images and identity build arguments. Keep both overlays in
`COMPOSE_FILE`; do not build or roll out with a reduced Compose file. Recheck
the production-only overlays and their hashes at the next release.

The identity applies to `web`, `catalog-web`, `telegram-bot`, and `max-bot`.
The helper verifies every requested service. `catalog-web` uses its public env
file; Telegram and MAX use the shared `.env` plus their own env files. The
explicit Compose service environment wins over all of those files. Pass the
corresponding Compose profiles when starting profiled services. The build and
identity checks do not start, recreate, or migrate containers.

## PRE and POST backup identity

The backup manifest takes `app_commit` from Django's
`settings.DENSTOCK_APP_COMMIT`. Before the first identity-aware release, the
currently running image can have correct code but a stale application identity
in its `.env`. Do not derive PRE's `app_commit` from that file. Set `BASE_SHA`
only after independently verifying the running image's code against the
accepted production commit. A legacy image label by itself is insufficient;
if its attestation is absent or differs from the actual code, stop and resolve
the identity before backup. Pass the verified SHA only to the backup process:

```bash
(
  exec 9>/run/lock/denstock-backup.lock
  flock -n 9 || exit 75
  docker compose exec -T -e DENSTOCK_APP_COMMIT="$BASE_SHA" web \
    python manage.py backup_all
)
```

This uses the existing web container, does not recreate a service, and does
not run migrations. It takes the same non-blocking host `flock` as the
scheduled wrapper and `backup_all` acquires the database consistency lock.
Without `--keep-last`, it does not prune. The command creates a signed local
run; verify it, then upload that exact run with the already approved
non-deleting copy/read-back procedure. Do not use the capped wrapper for this
one-time PRE: it currently invokes `backup_all --keep-last` and cannot pass the
per-process SHA override, so its manifest would inherit stale `.env` metadata.

After deployment, run the same locked, no-retention capture with
`BACKUP_SHA=<target SHA>` and `-e DENSTOCK_APP_COMMIT="$BACKUP_SHA"`; verify
that POST's manifest names the target. This explicit value is an extra check
even though the cleaned selector and Compose mapping already provide it.
For both PRE and POST, use the approved non-deleting copy/read-back procedure
for the exact run ID, then validate PostgreSQL, public media, private media,
signatures, hashes, references, and isolated restoration. If the required
lock, trusted runtime-code comparison, or signed upload path is unavailable,
stop before creating the backup and resolve that runbook gap separately; do
not change backup infrastructure as part of an application release.
