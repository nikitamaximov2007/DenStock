# Private media release: one Yandex bucket and encrypted Mac recovery points

Status: **operator procedure only; not authorization to run it**. Production
backup creation, file transfer, service changes and deployment remain stopped
until the owner approves the exact SHA and the production-changing steps.

This is the authoritative release procedure for application SHA
`e35948804069b3fe8ffa3f499fefe0a775541b47`. It keeps the existing Yandex
bucket `denstock-backups-nikita` for its normal daily schedule. Release PRE and
POST copies go to a new, separate directory on the owner's FileVault-protected
Mac and are encrypted with the existing age recovery recipient before being
written there. No second bucket, Yandex configuration change, upload, purge or
retention change is part of this release.

The PRE and POST backups are generated with the target backup code in a
one-off web container. The currently running web, catalog-web and bots remain
on their existing images until their approved service restart. `backup_all`
holds DenisStock's PostgreSQL exclusive backup lock while it captures the
database and both media trees. Do not use the old production backup wrapper:
it purges remote generations before verifying the new upload. Do not pass
`--keep-last` to `backup_all`; the default `None` disables local pruning.

## Read-only facts checked on 2026-10-17

These are a snapshot, not substitutes for the preflight immediately before an
approved release:

- Production HEAD and web runtime SHA: `f7b80861fc1d81f6be70c86e78be7f06418a82d2`.
- Web, catalog-web, Telegram bot, MAX bot, PostgreSQL 16 and proxy were running;
  application health and Django system check passed.
- Runtime identities at the read-only check:

  | Service | Runtime SHA | Image ID |
  |---|---|---|
  | `web` | `f7b80861fc1d81f6be70c86e78be7f06418a82d2` | `sha256:1874522c87c5bc6c6a7bb4dccca55d02a530f5d55371e04fc18f3b0d8079e99d` |
  | `catalog-web` | not exposed (`DENSTOCK_APP_COMMIT` unset) | `sha256:b941abf118173a6881bd8eb8f9d2c967fb72eda72408fffac1f128b329c438d7` |
  | `telegram-bot` | `952ee8eb2a95ec3bbbb467e3fda4eb486a1f54e2` | `sha256:a833c7e38490e5b7e4214893aedf3bd2baed284a2ebf0fb10928a0cbcaf4720a` |
  | `max-bot` | `1f6dc57c89c836e0845b4dc9e28cc36a2504762d` | `sha256:c9f211c36c9f3d4ad244b4cceac2ba8b557b1086a89b9a96edc330c01ef7df4c` |

  Save each original image ID and restore that exact image on rollback. Do not
  infer the catalog-web source SHA from the web SHA.
- All migrations shown on the current web were applied. The target has no
  migrations.
- Active Compose files were `docker-compose.yml`,
  `deploy/ai-support/docker-compose.external.yml` and the production-only,
  untracked `docker-compose.signing.yml`. Preserve that file.
- The live Caddy private attachment guard was present; anonymous probe returned
  404. Do not recreate or change proxy/Caddy.
- The 2026-10-16 03:00 MSK local/offsite run had a signature and was marked
  verified. The current web's `verify_backup` passed its checks; Yandex
  read-back matched all three files. It contained only `db.dump`,
  `media.tar.gz` and `manifest.json`, so it is not a complete private-media
  recovery point.
- The live private-media tree contained 16 regular files, 51,253,509 bytes and
  no symlinks. Recount at release time.
- Yandex is configured as `yandex-s3:denstock-backups-nikita`; the latest
  remote run did not contain `private_media.tar.gz`. No GitHub backup
  workflow/job exists in the production `f7b8086` tree, and no systemd/cron
  GitHub backup job is installed on the host.
- The Mac has FileVault enabled, age 1.3.2, `pg_restore` 16.15 and 274 GiB free.
  The existing recovery kit is about 103 MB. Its encrypted DB/media backup is
  dated 2026-10-11 03:00 MSK, while its encrypted private-media capture is from
  2026-10-11 20:40 MSK. Those separate captures are not one coherent recovery
  point. Preserve the kit and do not overwrite it.
- The nightly timer is enabled for 03:00 MSK. The local production filesystem
  had 8.3 GiB free and 31 backup directories. The current wrapper uses local
  `--keep-last 14` and can purge/clean hidden Yandex object versions.
- Current size estimate: DB plus public-media files are about 46.5 MB and the
  private-media tree is 51.3 MB before compression. Budget at least two actual
  full runs plus one restore scratch copy; measure each run before proceeding.
  The current Mac has ample space; the production filesystem had 8.3 GiB free.

## Release gate

Repeat and record all checks below at release time. Stop if any required value
differs unexpectedly:

1. Confirm `git rev-parse HEAD`, web runtime SHA, image IDs for all four web
   facing services, migration plan, `/healthz/`, database health and Caddy 404.
2. Confirm `e35948804069b3fe8ffa3f499fefe0a775541b47` is the approved candidate,
   descends from the current production SHA, has no migrations and changes only
   `web`, `telegram-bot` and `max-bot` at runtime.
3. Check free disk space, the current private-media file count/bytes, the
   nightly timer's next run, and `/run/lock/denstock-backup.lock`. Do not
   disable the timer or run through its 03:00 MSK window. Start only if the
   bounded release can finish and verify by 22:00 MSK.
4. On the Mac, recheck FileVault, age, PostgreSQL 16 tools and space. Create a
   unique release directory outside `~/DenisStock-Recovery`, with mode 700.
   Preserve every older encrypted generation.
5. Confirm the Yandex backup list before the release for later read-only
   comparison. Do not run the capped wrapper to create PRE or POST.

Read-only Mac checks:

```sh
fdesetup status
age --version
pg_restore --version
df -h "$HOME"
```

Read-only VPS timer/lock checks:

```sh
systemctl is-active denstock-backup.timer
systemctl list-timers denstock-backup.timer --no-pager
flock -n /run/lock/denstock-backup.lock -c true
```

If the PRE cannot be generated and read back on the Mac before deployment, do
not deploy. If POST verification cannot finish by the bounded window, stop the
release and use the app-only rollback below. Do not alter the nightly timer.

## PRE: full signed backup and encrypted Mac copy

The command is intentionally run from the candidate image as a one-off process,
before replacing any running service. Override the manifest's `app_commit` with
the actual source runtime SHA so it identifies the application state being
protected. Record the backup-tool SHA separately in the release journal.

After owner approval, on `/opt/denstock`:

```sh
SOURCE_SHA=$(git rev-parse HEAD)
test "$SOURCE_SHA" = f7b80861fc1d81f6be70c86e78be7f06418a82d2 || exit 1
RELEASE_SHA=e35948804069b3fe8ffa3f499fefe0a775541b47

# Inventory and preserve production-only untracked files. Never git-clean them.
git status --short
git diff --quiet && git diff --cached --quiet || exit 1

# Record and tag the exact existing image IDs before building over local tags.
ROLLBACK_ID=$(date -u +%Y%m%dT%H%M%SZ)
for service in web telegram-bot max-bot; do
    cid=$(docker compose --profile telegram-bot --profile max-bot ps -q "$service")
    test -n "$cid" || exit 1
    image_id=$(docker inspect --format '{{.Image}}' "$cid")
    docker image tag "$image_id" "denstock-${service}:rollback-${ROLLBACK_ID}"
done

git fetch origin
git cat-file -e "$RELEASE_SHA^{commit}" || exit 1
git merge-base --is-ancestor "$SOURCE_SHA" "$RELEASE_SHA" || exit 1
git checkout --detach "$RELEASE_SHA"
test "$(git rev-parse HEAD)" = "$RELEASE_SHA" || exit 1
docker compose --profile telegram-bot --profile max-bot build web

# No entrypoint, web server, dependencies, migration or retention wrapper.
# Do not add --keep-last. Copy the printed run ID into RUN_ID below.
docker compose --profile telegram-bot --profile max-bot run --rm -T --no-deps \
    -e "DENSTOCK_APP_COMMIT=$SOURCE_SHA" --entrypoint python web \
    manage.py backup_all --trigger manual
RUN_ID='<printed backup run ID>'

docker compose --profile telegram-bot --profile max-bot run --rm -T --no-deps \
    -e "DENSTOCK_APP_COMMIT=$SOURCE_SHA" --entrypoint python web \
    manage.py verify_backup "$RUN_ID"
```

Require all of the following before transfer: manifest signature and every
component hash pass; `private_media_status=included`; the dump is fully
decodable by PostgreSQL 16; both media archives validate; the captured file
count/tree hash match the manifest; no referenced private attachment is
missing. The generation is additive under `backups/`; no prior local run is
removed. It is not uploaded to Yandex.

On the Mac, use the existing streaming capture helper. It hashes and parses
the stream in memory, checks the source tree before and after, and writes only
age ciphertext. Its temporary `.partial` is removed on failure. Use a new
release directory and unique output name; never overwrite the recovery kit:

```sh
umask 077
set -e
RELEASE_ID=$(date -u +%Y%m%dT%H%M%SZ)
RUN_ID='<exact run ID printed by the PRE command on the VPS>'
PHASE=PRE # set to POST for the second recovery point
DEST="$HOME/DenisStock-Release-Recovery/$RELEASE_ID"
mkdir -m 700 -p "$DEST"
python3 "$HOME/DenisStock-Recovery/tools/capture.py" \
    /opt/denstock/backups "$RUN_ID" \
    "$DEST/DenisStock-$PHASE-$RUN_ID.tar.age" \
    "$HOME/DenisStock-Recovery/recipient.txt" tree \
    > "$DEST/DenisStock-$PHASE-$RUN_ID.capture.json"
test -s "$DEST/DenisStock-$PHASE-$RUN_ID.tar.age" && \
    test -s "$DEST/DenisStock-$PHASE-$RUN_ID.tar.age.inventory.age" && \
    test -s "$DEST/DenisStock-$PHASE-$RUN_ID.capture.json"
shasum -a 256 "$DEST/DenisStock-$PHASE-$RUN_ID.tar.age"
```

The capture helper uses the existing SSH identity and public age recipient;
never copy the age identity or production signing private key to the VPS.
Keep the JSON capture summary with the encrypted archive. It records the
plaintext stream hash, ciphertext hash, byte counts and tree comparison.
The helper also creates an encrypted `.inventory.age` file beside the archive;
preserve it with the capture record. Record the source runtime SHA, backup-tool
SHA, target SHA, backup run ID, timestamp and both capture hashes in a
non-secret release record under this new directory.

For each PRE and POST copy, validate the exact Mac ciphertext, then restore the
decrypted copy only in FileVault-protected scratch and an isolated local
PostgreSQL 16 container. The decryption identity remains encrypted on the Mac;
age prompts for its passphrase. The helper's stream hash ties the decrypted
outer archive to the exact source bytes that were signature-verified on the
VPS:

```sh
PHASE=PRE # use POST for the second recovery point
RUN_ID='<exact run ID>'
RELEASE_SHA=e35948804069b3fe8ffa3f499fefe0a775541b47
CANDIDATE_CHECKOUT='<clean Mac checkout of the approved candidate>'
CAPTURE="$DEST/DenisStock-$PHASE-$RUN_ID.capture.json"
CIPHER="$DEST/DenisStock-$PHASE-$RUN_ID.tar.age"
EXPECTED_STREAM=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["stream_sha256"])' "$CAPTURE")
EXPECTED_CIPHER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ciphertext_sha256"])' "$CAPTURE")
set -e
test "$(shasum -a 256 "$CIPHER" | awk '{print $1}')" = "$EXPECTED_CIPHER" || exit 1

SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/denstock-release-verify.XXXXXX")
chmod 700 "$SCRATCH"
PG_CONTAINER=""
cleanup() {
    if [ -n "$PG_CONTAINER" ]; then docker stop "$PG_CONTAINER" >/dev/null 2>&1 || true; fi
    python3 - "$SCRATCH" <<'PY'
import shutil
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
if path.name.startswith("denstock-release-verify.") and path.is_dir():
    shutil.rmtree(path)
PY
}
trap cleanup EXIT
age -d -i <(age -d "$HOME/DenisStock-Recovery/keys/recovery-identity.age") \
    "$CIPHER" > "$SCRATCH/bundle.tar"
test "$(shasum -a 256 "$SCRATCH/bundle.tar" | awk '{print $1}')" = "$EXPECTED_STREAM" || exit 1
tar -xf "$SCRATCH/bundle.tar" -C "$SCRATCH"
RUN_DIR="$SCRATCH/$RUN_ID"
test -f "$RUN_DIR/manifest.json" && test -f "$RUN_DIR/db.dump" && \
    test -f "$RUN_DIR/media.tar.gz" && test -f "$RUN_DIR/private_media.tar.gz" || exit 1
export RUN_DIR SCRATCH
```

Before this local restore, `verify_backup "$RUN_ID"` on the VPS must pass
again. The captured stream hash and encrypted file hash must match the saved
summary. The manifest and every inner component are therefore byte-identical
to the signed, verified source run.

Use a new uniquely named container for each phase. Its only published port is
bound to loopback. The container has synthetic data, no production credentials
and no persistent Docker volume:

```sh
PG_CONTAINER="denstock-release-restore-$PHASE-$RELEASE_ID"
docker run --rm -d --name "$PG_CONTAINER" --network bridge \
    -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1::5432 postgres:16
PG_PORT=$(docker inspect --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' "$PG_CONTAINER")
for attempt in $(seq 1 45); do
    docker exec "$PG_CONTAINER" pg_isready -U postgres && break
    test "$attempt" -lt 45 || exit 1
    sleep 1
done
docker exec "$PG_CONTAINER" psql -U postgres -c 'CREATE ROLE denstock_public NOLOGIN'
docker exec -i "$PG_CONTAINER" pg_restore --no-owner -U postgres -d postgres < "$RUN_DIR/db.dump"
mkdir -m 700 "$SCRATCH/public" "$SCRATCH/private"
tar -xzf "$RUN_DIR/media.tar.gz" -C "$SCRATCH/public"
```

Run the following from a clean checkout of the approved candidate SHA, with
`RUN_DIR`, `SCRATCH`, `PG_PORT` and `CANDIDATE_CHECKOUT` set as above. It uses
only the local restore container and prints aggregate counts, never file names
or customer rows:

```sh
cd "$CANDIDATE_CHECKOUT"
test "$(git rev-parse HEAD)" = "$RELEASE_SHA" || exit 1
DJANGO_SETTINGS_MODULE=config.settings.test \
DENSTOCK_TEST_DATABASE_URL="postgresql://postgres@127.0.0.1:$PG_PORT/postgres" \
DENSTOCK_MEDIA_ROOT="$SCRATCH/public" \
PRIVATE_MEDIA_ROOT="$SCRATCH/private" \
python manage.py shell <<'PY'
import json
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db.models import FileField

from apps.ai_support.files import private_path
from apps.ai_support.models import SupportAttachment
from apps.catalog_import.models import CatalogImportBatch
from apps.operations.emergency_state import media_tree_sha256
from apps.operations.private_media import inspect_tree, restore_archive, Inventory

run = Path(__import__("os").environ["RUN_DIR"])
private_root = Path(settings.PRIVATE_MEDIA_ROOT)
manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
expected = Inventory(
    int(manifest["private_media_file_count"]),
    int(manifest["private_media_bytes"]),
    manifest["private_media_tree_sha256"],
)
actual = restore_archive(run / "private_media.tar.gz", private_root, expected=expected)
if actual != expected or inspect_tree(private_root, require_present=True) != expected:
    raise SystemExit("private_media tree differs from signed manifest")
if media_tree_sha256(settings.MEDIA_ROOT) != manifest["media_tree_sha256"]:
    raise SystemExit("public media tree differs from signed manifest")

checked = missing = 0
for model in apps.get_models():
    for field in model._meta.local_fields:
        if not isinstance(field, FileField):
            continue
        names = model._default_manager.exclude(**{field.name: ""}).values_list(field.name, flat=True)
        for name in names.iterator(chunk_size=1000):
            if name:
                checked += 1
                missing += not field.storage.exists(name)
for path in SupportAttachment.objects.values_list("relative_path", flat=True).iterator(chunk_size=1000):
    checked += 1
    try:
        missing += not private_path(path).is_file()
    except ValueError:
        missing += 1
catalog_root = (private_root / "catalog-imports").resolve()
for name in CatalogImportBatch.objects.exclude(stored_path="").values_list(
    "stored_path", flat=True
).iterator(chunk_size=1000):
    target = (catalog_root / name).resolve()
    checked += 1
    missing += not (catalog_root in target.parents and target.is_file())
print(f"media trees verified; file references checked={checked}; missing={missing}")
if missing:
    raise SystemExit(1)
PY
```

The shell trap stops only the container created above and removes only the
unique scratch directory created by `mktemp`, including on failure. Do not
print database rows, attachment names or customer data.

If age decryption, either hash, signature, PostgreSQL restore, media extraction,
private tree comparison or the file-reference audit fails, do not treat that
phase as a recovery point and do not deploy/qualify the release.

## Deploy the approved application SHA

Proceed only after PRE has been verified both on the VPS and from the encrypted
Mac copy. Confirm again that the target SHA is exact and no migration is
pending. Preserve the original image IDs above. Build and recreate only the
three approved services, with their Compose profiles and `--no-deps`:

```sh
python3 - "$RELEASE_SHA" <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
indexes = [i for i, line in enumerate(lines) if line.startswith("DENSTOCK_APP_COMMIT=")]
if len(indexes) != 1:
    raise SystemExit("Expected exactly one DENSTOCK_APP_COMMIT in .env; stop.")
lines[indexes[0]] = f"DENSTOCK_APP_COMMIT={sys.argv[1]}"
temporary = path.with_name(".env.release-tmp")
if temporary.exists():
    raise SystemExit(".env.release-tmp already exists; inspect it and stop.")
temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
metadata = path.stat()
os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
os.chown(temporary, metadata.st_uid, metadata.st_gid)
os.replace(temporary, path)
PY

docker compose --profile telegram-bot --profile max-bot build web telegram-bot max-bot
docker compose --profile telegram-bot --profile max-bot up -d --no-deps --wait \
    --force-recreate web telegram-bot max-bot
```

Do not recreate PostgreSQL, `catalog-web` or proxy/Caddy. Do not run
`migrate`. Verify each runtime SHA/image, bot health/heartbeat, `/healthz/`,
`manage.py check`, zero pending migrations, web-restore denial, and the Caddy
anonymous attachment 404. Confirm the container IDs for PostgreSQL,
`catalog-web` and proxy did not change.

## POST: full signed backup and encrypted Mac copy

Use the deployed web image directly. This is the first release backup expected
to include private media through the normal app backup path:

```sh
docker compose exec -T web python manage.py backup_all --trigger manual
RUN_ID='<printed backup run ID>'
docker compose exec -T web python manage.py verify_backup "$RUN_ID"
```

Do not pass `--keep-last`, and do not call
`/usr/local/sbin/denstock-backup-capped`, `backup_offsite.sh`, `rclone sync`,
`rclone purge` or `cleanup-hidden`. The new local generation is additive. Repeat
the Mac `capture.py` command with `PHASE=POST` and the new run ID. Confirm the
Mac ciphertext SHA against the capture summary and repeat the
decryption and isolated restore procedure above with `PHASE=POST`. Preserve the
encrypted `.inventory.age` generated by the helper. Remove the temporary
plaintext directory after the recovery checks. Do not write plaintext customer
files outside FileVault-protected storage.

Restore PRE and POST separately into disposable PostgreSQL 16 databases and
fresh public/private media directories on the Mac. Restore no copy over a
production database or media volume. Require successful full PostgreSQL
restore, archive extraction, signature/hash validation and zero missing
database references to private files. Keep only the encrypted `.age` files,
their non-secret capture summaries and release record after cleanup. If local
restore cannot be completed, the release is not qualified.

After POST acceptance, check that the daily timer remains enabled and that the
next normal Yandex run succeeds with `db.dump`, `media.tar.gz`,
`private_media.tar.gz` and a signed manifest. Verify actual remote read-back of
every file for that ordinary run, without changing the remote:

```sh
RUN_ID=$(python3 -c 'import json; print(json.load(open("backups/offsite_status.json"))["uploaded_run"])')
test -n "$RUN_ID"
docker compose exec -T web python manage.py verify_backup "$RUN_ID"
rclone check --download "backups/$RUN_ID" "yandex-s3:denstock-backups-nikita/$RUN_ID"
rclone lsf "yandex-s3:denstock-backups-nikita/$RUN_ID" --files-only | LC_ALL=C sort
```

The remote listing must contain the manifest, database dump, public archive
and private archive. The capped wrapper's existing Yandex retention remains in
effect; it is not a durable archive. The independent
encrypted Mac PRE/POST copies are the release recovery points. Do not install
GitHub Releases backup in this release.

## Rollback and stop conditions

There are no application migrations. Before build, record each service's
original image ID and its own `DENSTOCK_APP_COMMIT`; the bot SHAs currently
differ from web. For an app-only rollback, restore the exact three image IDs
from their rollback tags, then recreate each service separately with the
matching original commit value in `.env`:

```sh
set -e
for required in WEB_OLD_SHA TELEGRAM_OLD_SHA MAX_OLD_SHA ROLLBACK_ID; do
    test -n "${!required:-}" || exit 1
done
for service in web telegram-bot max-bot; do
    docker image tag "denstock-${service}:rollback-${ROLLBACK_ID}" \
        "denstock-${service}:latest" || exit 1
done
set_commit() {
    python3 - "$1" <<'PY'
import os, stat, sys
from pathlib import Path
path=Path(".env")
lines=path.read_text(encoding="utf-8").splitlines()
indexes=[i for i,line in enumerate(lines) if line.startswith("DENSTOCK_APP_COMMIT=")]
if len(indexes) != 1: raise SystemExit("Expected one DENSTOCK_APP_COMMIT; stop.")
lines[indexes[0]]=f"DENSTOCK_APP_COMMIT={sys.argv[1]}"
temporary=path.with_name(".env.release-tmp")
if temporary.exists(): raise SystemExit(".env.release-tmp exists; inspect it and stop.")
temporary.write_text("\n".join(lines)+"\n",encoding="utf-8")
metadata=path.stat()
os.chmod(temporary,stat.S_IMODE(metadata.st_mode))
os.chown(temporary,metadata.st_uid,metadata.st_gid)
os.replace(temporary,path)
PY
}
for service in web telegram-bot max-bot; do
    case "$service" in
        web) sha="$WEB_OLD_SHA" ;;
        telegram-bot) sha="$TELEGRAM_OLD_SHA" ;;
        max-bot) sha="$MAX_OLD_SHA" ;;
    esac
    set_commit "$sha"
    docker compose --profile telegram-bot --profile max-bot up -d --no-deps --no-build \
        --force-recreate "$service"
done
set_commit "$WEB_OLD_SHA" # restore the original global .env value after bot rollback
```

Keep PostgreSQL and all data volumes running. Do not restore the PRE database
for a code-only failure. A full data restore is a separate disaster-recovery
action requiring explicit owner approval.

Stop without proceeding if any SHA/image is unexpected; the signature or a
hash fails; private media is absent or changes during capture; the Mac copy
cannot decrypt or restore; disk space is insufficient; the nightly timer or
backup lock could overlap; the old images cannot be restored exactly; an
unexpected migration appears; Yandex or Caddy configuration would need
changing; or a step would delete, overwrite or rewrite an existing generation.

This runbook does not create a second bucket, alter Yandex lifecycle/retention,
install GitHub backup, change production now, or authorize any production
operation. Ask the owner to approve the exact candidate and release window
after all local qualification gates pass.
