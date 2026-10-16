#!/usr/bin/env bash
# One release-only signed backup with no local or offsite retention.
# Run from an approved operator session; this does not replace the daily wrapper.
set -euo pipefail
set +x
umask 077

phase="${1:-}"
[[ "$phase" == pre || "$phase" == post ]] || {
    echo "usage: private_media_release_backup_no_retention.sh pre|post" >&2
    exit 2
}

baseline_sha=f7b80861fc1d81f6be70c86e78be7f06418a82d2
release_sha=e35948804069b3fe8ffa3f499fefe0a775541b47
expected_sha="$baseline_sha"
[[ "$phase" == post ]] && expected_sha="$release_sha"

# The overrides exist only for disposable synthetic tests. Production always
# uses the same lock and backup directory as denstock-backup-capped.
if [[ "${DENSTOCK_RELEASE_SYNTHETIC:-0}" == 1 ]]; then
    root="${DENSTOCK_RELEASE_TEST_ROOT:?synthetic root required}"
    lock_file="${DENSTOCK_RELEASE_TEST_LOCK:?synthetic lock required}"
else
    root=/opt/denstock
    lock_file=/run/lock/denstock-backup.lock
fi
cd "$root"
[[ -d backups && -f .env.backup ]] || { echo "backup root/config missing" >&2; exit 1; }

exec 9>"$lock_file"
flock -n 9 || { echo "backup lock busy; STOP" >&2; exit 75; }

set -a
# shellcheck disable=SC1091
. ./.env.backup
set +a
[[ "${BACKUP_OFFSITE_ENABLED:-}" == true && "${BACKUP_OFFSITE_METHOD:-}" == rclone ]] || {
    echo "offsite configuration is not enabled for rclone" >&2
    exit 1
}
target="${BACKUP_OFFSITE_TARGET:-}"
[[ -n "$target" && "$target" == *:* && "$target" != *$'\n'* ]] || {
    echo "offsite target missing or malformed" >&2
    exit 1
}
target="${target%/}"

if [[ "${DENSTOCK_RELEASE_SYNTHETIC:-0}" != 1 ]]; then
    [[ "$(git rev-parse HEAD)" == "$expected_sha" ]] || {
        echo "production checkout SHA changed; STOP" >&2
        exit 1
    }
    runtime_sha="$(docker compose exec -T web python -c \
        'import os; print(os.environ.get("DENSTOCK_APP_COMMIT", ""))' </dev/null | tr -d '\r')"
    [[ "$runtime_sha" == "$expected_sha" ]] || {
        echo "web runtime SHA changed; STOP" >&2
        exit 1
    }
fi

remote_bytes() {
    rclone size --s3-versions "$target" --json </dev/null |
        python3 -c 'import json,sys; print(int(json.load(sys.stdin)["bytes"]))'
}

no_unfinished_uploads() {
    rclone backend list-multipart-uploads "$target" </dev/null |
        python3 -c 'import json,sys; x=json.load(sys.stdin); assert isinstance(x,dict); assert sum(len(v) for v in x.values()) == 0, "unfinished multipart uploads; STOP"'
}

no_unfinished_uploads
start_remote_bytes="$(remote_bytes)"
[[ "$start_remote_bytes" =~ ^[0-9]+$ ]] || exit 1

# Require a conservative local buffer for a new run plus the later POST and
# temporary verification. The actual archive size is checked after creation.
latest_run="$(find backups -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | LC_ALL=C sort | tail -1)"
[[ -n "$latest_run" ]] || { echo "no existing backup baseline" >&2; exit 1; }
latest_bytes="$(python3 - "backups/$latest_run" <<'PY'
from pathlib import Path
import sys
print(sum(p.stat().st_size for p in Path(sys.argv[1]).rglob("*") if p.is_file()))
PY
)"
private_bytes="$(docker compose exec -T web du -sb /app/private_media </dev/null | cut -f1 | tr -d '\r')"
free_bytes="$(python3 -c 'import shutil; print(shutil.disk_usage(".").free)')"
if [[ "${DENSTOCK_RELEASE_SYNTHETIC:-0}" == 1 && -n "${DENSTOCK_RELEASE_SYNTHETIC_FREE_BYTES:-}" ]]; then
    free_bytes="$DENSTOCK_RELEASE_SYNTHETIC_FREE_BYTES"
fi
[[ "$latest_bytes" =~ ^[0-9]+$ && "$private_bytes" =~ ^[0-9]+$ && "$free_bytes" =~ ^[0-9]+$ ]] || exit 1
needed_bytes=$((3 * (latest_bytes + private_bytes + 134217728)))
(( free_bytes > needed_bytes )) || { echo "insufficient local space; STOP" >&2; exit 1; }

before="$(find backups -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | LC_ALL=C sort)"
# Deliberately OMIT --keep-last. backup_all prunes only when that argument is truthy.
docker compose exec -T web python manage.py backup_all --trigger manual </dev/null
after="$(find backups -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | LC_ALL=C sort)"
run_id="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after"))"
[[ -n "$run_id" && "$run_id" != *$'\n'* ]] || {
    echo "expected exactly one new run; STOP" >&2
    exit 1
}
[[ "$run_id" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]] || exit 1
run_path="backups/$run_id"

docker compose exec -T web python manage.py verify_backup "$run_id" </dev/null
docker compose exec -T -e RELEASE_RUN_ID="$run_id" -e RELEASE_PHASE="$phase" \
    -e RELEASE_EXPECTED_SHA="$expected_sha" web python manage.py shell <<'PY'
import json
import os
from pathlib import Path

from apps.operations import backup
from apps.operations.emergency_manifest import validate_manifest
from apps.operations.manifest_signing import verify_manifest

run = backup.backup_root() / os.environ["RELEASE_RUN_ID"]
manifest = json.loads((run / "manifest.json").read_text())
verify_manifest(manifest)
assert manifest["signature"]["key_id"] == "production-1"
assert manifest["app_commit"] == os.environ["RELEASE_EXPECTED_SHA"]
assert validate_manifest(run, expected_source="production").ok
expected = {"db.dump", "media.tar.gz"}
if os.environ["RELEASE_PHASE"] == "post":
    from apps.operations.private_media import archive_inventory

    expected.add("private_media.tar.gz")
    assert manifest["private_media_status"] == "included"
    inventory = archive_inventory(run / "private_media.tar.gz")
    assert inventory.files == manifest["private_media_file_count"]
    assert inventory.bytes == manifest["private_media_bytes"]
    assert inventory.tree_sha256 == manifest["private_media_tree_sha256"]
else:
    assert manifest.get("private_media_status", "absent") == "absent"
assert set(manifest["sha256"]) == expected
assert {p.name for p in run.iterdir() if p.is_file()} == expected | {"manifest.json"}
backup.verify_database_payload(run / "db.dump", "postgresql")
backup.verify_media_payload(run / "media.tar.gz")
print("SIGNED_BACKUP_COMPONENTS_PASS")
PY

run_bytes="$(python3 - "$run_path" <<'PY'
from pathlib import Path
import sys
print(sum(p.stat().st_size for p in Path(sys.argv[1]).rglob("*") if p.is_file()))
PY
)"
[[ "$run_bytes" =~ ^[0-9]+$ ]] || exit 1
no_unfinished_uploads
current_remote_bytes="$(remote_bytes)"
# Published bucket cap is 921,600,000 bytes. Stay below its 800 MiB soft
# threshold even with one duplicate upload and 128 MiB of versioning margin.
(( current_remote_bytes + 2 * run_bytes + 134217728 <= 838860800 )) || {
    echo "insufficient offsite headroom; old copies preserved; STOP" >&2
    exit 1
}

remote_listing="$(rclone lsf "$target" --dirs-only --max-depth 1 </dev/null)"
if printf '%s\n' "$remote_listing" | grep -Fxq "$run_id/"; then
    echo "remote run prefix already exists; STOP" >&2
    exit 1
fi
destination="$target/$run_id"
# No sync, purge, cleanup, delete or move. --immutable refuses overwrites.
rclone copy --immutable "$run_path" "$destination" </dev/null
rclone check --download "$run_path" "$destination" </dev/null
no_unfinished_uploads
final_remote_bytes="$(remote_bytes)"
(( final_remote_bytes <= 838860800 )) || { echo "offsite soft limit exceeded; STOP" >&2; exit 1; }
echo "NO_RETENTION_RELEASE_BACKUP_PASS phase=$phase run_id=$run_id bytes=$run_bytes remote_before=$start_remote_bytes remote_after=$final_remote_bytes"
