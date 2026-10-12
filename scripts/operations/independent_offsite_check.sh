#!/usr/bin/env bash
# Freshness and integrity check of the INDEPENDENT encrypted copies.
# Runs from anywhere that is NOT the production VPS (the owner's Mac, a second
# server): it only needs read access to the independent target.
#
#   independent_offsite_check.sh --target remote:path --config rclone.conf
#                                [--max-age-hours 26] [--deep]
# Exit 0 fresh, 2 STALE or none, 1 integrity failure / unreachable.
set -euo pipefail
TARGET="" CONF="" MAX_AGE=26 DEEP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --config) CONF="$2"; shift 2 ;;
        --max-age-hours) MAX_AGE="$2"; shift 2 ;;
        --deep) DEEP=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$TARGET" && -f "$CONF" ]] || { echo "--target and --config are required" >&2; exit 2; }
# Bounded: an unreachable provider must turn into an alert, not an endless retry.
RCLONE=(rclone --config "$CONF" --contimeout 15s --timeout 2m --retries 2 --low-level-retries 3)
LISTING="$("${RCLONE[@]}" lsf "$TARGET" --files-only)" || { echo "UNREACHABLE $TARGET"; exit 1; }
# No match is a normal answer ("no confirmed copy"), not a crash: || true.
NEWEST="$(printf '%s\n' "$LISTING" | { grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}\.signed-backup\.tar\.age\.json$' || true; } | LC_ALL=C sort | tail -1)"
[[ -n "$NEWEST" ]] || { echo "STALE no confirmed independent copy"; exit 2; }
SIDECAR="$("${RCLONE[@]}" cat "$TARGET/$NEWEST")" || { echo "UNREADABLE $NEWEST"; exit 1; }
VERDICT="$(SIDECAR="$SIDECAR" MAX_AGE="$MAX_AGE" python3 - <<'PY'
import json, os
from datetime import datetime, timezone
s = json.loads(os.environ["SIDECAR"])
created = datetime.fromisoformat(s["created_at"])
age = (datetime.now(timezone.utc) - created).total_seconds() / 3600
print(f"{'FRESH' if age <= float(os.environ['MAX_AGE']) else 'STALE'} {s['run']} "
      f"age_hours={age:.1f} sha={s['ciphertext_sha256']}")
PY
)"
echo "$VERDICT"
if [[ "$DEEP" == 1 ]]; then
    RUN="$(echo "$VERDICT" | awk '{print $2}')"
    EXPECTED="$(echo "$VERDICT" | sed -E 's/.*sha=//')"
    ACTUAL="$("${RCLONE[@]}" cat "$TARGET/$RUN.signed-backup.tar.age" | shasum -a 256 | cut -c1-64)"
    if [[ "$ACTUAL" != "$EXPECTED" ]]; then echo "CORRUPT $RUN ciphertext differs from its sidecar"; exit 1; fi
    echo "INTACT $RUN"
fi
[[ "$VERDICT" == FRESH* ]] || exit 2
