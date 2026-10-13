#!/bin/bash
# Owner's Mac: daily FRESH/STALE/CORRUPT check of the GitHub backup copies with a
# macOS notification on any problem, plus a weekly COLD COPY of the newest
# encrypted generation (ciphertext only) outside GitHub. Read-only token.
#   ~/.config/denisstock/github-backup-monitor.env (0600):
#     GHB_REPO=<owner>/<backup repo>   GHB_TOKEN_FILE=<read-only token file>
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
JOB="$HERE/github_release_backup.py"
export GHB_CONFIG="${GHB_CONFIG:-$HOME/.config/denisstock/github-backup-monitor.env}"
LOG="$HOME/Library/Logs/denisstock-github-backup.log"
COLD="${COLD_DIR:-$HOME/DenisStock-Recovery-Updates/github}"
DEEP=()
[[ "$(date +%u)" == 1 ]] && DEEP=(--deep)
OUT="$(/usr/bin/python3 "$JOB" check ${DEEP[@]+"${DEEP[@]}"} 2>&1)"; CODE=$?
printf '%s %s\n' "$(date '+%F %T')" "$OUT" >> "$LOG"
if [[ $CODE -ne 0 ]]; then
    osascript -e 'on run argv' \
        -e 'display notification (item 1 of argv) with title "DenisStock: копии на GitHub"' \
        -e 'end run' "$OUT"
fi
if [[ "$(date +%u)" == 1 ]]; then
    DEST="$COLD/$(date +%F)"
    if [[ ! -e "$DEST" ]]; then
        mkdir -p -m 700 "$COLD"
        if ! /usr/bin/python3 "$JOB" fetch --out "$DEST" >> "$LOG" 2>&1; then
            osascript -e 'display notification "Холодная копия с GitHub не скачалась" with title "DenisStock"'
        fi
    fi
fi
exit $CODE
