#!/usr/bin/env bash
# One-command REAL-DATA recovery drill on the owner's Mac.
#
#   scripts/dr/morning_real_drill.sh [--kit <dir>] [--secrets <production-secrets-*.tar.age>]
#
# The owner types the recovery passphrase into age (never an argument). The kit
# is only READ. Decrypted data lives in a new 0700 directory under $HOME
# (FileVault) and is removed on exit together with the isolated dr-real-*
# stack, even after a failure. Only the JSON report (timings, check results,
# object counts; no secrets, no customer content) is kept, in
# ~/DenisStock-DR-reports/. Nothing talks to production or to Yandex.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
KIT="$HOME/DenisStock-Recovery-Updates/2026-10-12"
SECRETS="$(ls -1 "$HOME"/DenisStock-Recovery/secrets/production-secrets-*.tar.age | sort | tail -1)"
IDENTITY="$HOME/DenisStock-Recovery/keys/recovery-identity.age"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kit) KIT="$2"; shift 2 ;;
        --secrets) SECRETS="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
STAMP="$(date -u +%Y%m%d%H%M%S)"
PROJECT="dr-real-$STAMP"
WORK="$HOME/DenisStock-DR-work.$STAMP"
REPORTS="$HOME/DenisStock-DR-reports"
PORT="${DR_PORT:-18090}"
export DR_PYTHON="${DR_PYTHON:-$(cd "$HERE/../.." && pwd -P)/.venv/bin/python}"
[[ -x "$DR_PYTHON" ]] || DR_PYTHON=python3
cleanup() {
    local code=$?
    "$HERE/cleanup_dr.sh" "$PROJECT" >/dev/null 2>&1 || true
    rm -rf "$WORK"
    [[ ! -e "$WORK" ]] && echo "[real-drill] decrypted data removed" >&2
    exit "$code"
}
trap cleanup EXIT
mkdir -p -m 700 "$REPORTS"
START=$(date +%s)
"$HERE/prepare_kit_restore.sh" --kit "$KIT" --identity "$IDENTITY" --work "$WORK" --secrets "$SECRETS"
DECRYPTED=$(date +%s)
PRIVATE_ARGS=()
[[ -f "$WORK/private.tar" ]] && PRIVATE_ARGS=(--private-archive "$WORK/private.tar"
    --private-inventory "$WORK/private-inventory.json")
"$HERE/offline_restore.sh" --project "$PROJECT" --source "$WORK/source" --run "$WORK/run" \
    ${PRIVATE_ARGS[@]+"${PRIVATE_ARGS[@]}"} --env-dir "$WORK/env" \
    --pubkey "$WORK/pinned-public.pem" --port "$PORT" --mode test --report "$WORK/report.json"
END=$(date +%s)
"$DR_PYTHON" - "$WORK/report.json" "$REPORTS/$PROJECT.json" "$((DECRYPTED - START))" "$((END - START))" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
report["decrypt_seconds_including_passphrase"] = int(sys.argv[3])
report["total_seconds"] = int(sys.argv[4])
json.dump(report, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
print(f"[real-drill] passed={report.get('passed')} total={sys.argv[4]}s report={sys.argv[2]}")
PY
