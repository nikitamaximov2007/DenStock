#!/usr/bin/env bash
# One-command REAL-DATA recovery drill on the owner's Mac.
#
#   scripts/dr/morning_real_drill.sh [--kit <dir>] [--secrets <encrypted secrets>]
#     [--fresh-secrets --pubkey <pinned public key> --source-git <source Git URL>]
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
SECRETS=""
IDENTITY="$HOME/DenisStock-Recovery/keys/recovery-identity.age"
PLAIN=() SOURCE_GIT=() FRESH=() PUBKEY=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kit) KIT="$2"; shift 2 ;;
        --secrets) SECRETS="$2"; shift 2 ;;
        --fresh-secrets) FRESH=(--fresh-secrets); shift ;;
        --pubkey) PUBKEY=(--pubkey "$2"); shift 2 ;;
        --identity) IDENTITY="$2"; shift 2 ;;
        # A kit fetched from GitHub has no source bundle: clone all remote branches.
        --source-git) SOURCE_GIT=(--source-git "$2"); shift 2 ;;
        # Synthetic rehearsal of this exact procedure with an unprotected TEST key.
        --plain-identity) PLAIN=(--plain-identity); shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ${#FRESH[@]} -eq 0 && -z "$SECRETS" ]]; then
    SECRETS="$(ls -1 "$HOME"/DenisStock-Recovery/secrets/production-secrets-*.tar.age 2>/dev/null |
        sort | tail -1)"
fi
STAMP="$(date -u +%Y%m%d%H%M%S)"
PROJECT="dr-real-$STAMP"
WORK="$HOME/DenisStock-DR-work.$STAMP"
REPORTS="${DR_REPORTS_DIR:-$HOME/DenisStock-DR-reports}"
PORT="${DR_PORT:-18090}"
die() { echo "[real-drill] STOP: $*" >&2; exit 1; }

# Preflight BEFORE the passphrase is asked: everything that can fail without
# decrypting anything fails here.
# 1. A Python that can verify Ed25519 (offline_restore step 1). macOS python3
#    has no cryptography; the project's shared venv does.
if [[ -z "${DR_PYTHON:-}" ]]; then
    for candidate in "$(cd "$HERE/../.." && pwd -P)/.venv/bin/python" \
                     "$HOME/Developer/DenStock/.venv/bin/python" python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import cryptography" 2>/dev/null; then
            DR_PYTHON="$candidate"; break
        fi
    done
fi
[[ -n "${DR_PYTHON:-}" ]] && "$DR_PYTHON" -c "import cryptography" 2>/dev/null ||
    die "no Python with the cryptography package (set DR_PYTHON)"
export DR_PYTHON
# 2. Tools and Docker.
for tool in age docker git lsof; do command -v "$tool" >/dev/null || die "$tool is not installed"; done
docker info >/dev/null 2>&1 || die "Docker is not running"
# 3. Leftovers of an interrupted earlier drill (SIGKILL, power loss) are removed
#    first, by hand: this drill never starts next to old decrypted data.
LEFTOVER_DIRS=$(ls -d "$HOME"/DenisStock-DR-work.* 2>/dev/null || true)
LEFTOVER_VOLUMES=$(docker volume ls -q | grep '^dr-real-' || true)
if [[ -n "$LEFTOVER_DIRS$LEFTOVER_VOLUMES" ]]; then
    echo "[real-drill] leftovers of an earlier drill:" >&2
    printf '  %s\n' $LEFTOVER_DIRS $LEFTOVER_VOLUMES >&2
    die "remove them first: rm -rf ~/DenisStock-DR-work.*; scripts/dr/cleanup_dr.sh <dr-real-project>"
fi
# 4. The kit is only read: its file hashes are compared before and after.
[[ -d "$KIT" && -f "$IDENTITY" ]] || die "kit or identity not found"
if [[ ${#FRESH[@]} -eq 1 ]]; then
    [[ ${#PUBKEY[@]} -eq 2 && -f "${PUBKEY[1]}" && ${#SOURCE_GIT[@]} -eq 2 ]] ||
        die "--fresh-secrets requires --pubkey and --source-git"
    [[ -z "$SECRETS" ]] || die "--fresh-secrets excludes --secrets"
else
    [[ -f "$SECRETS" ]] || die "secrets archive not found"
fi
kit_hashes() { (
    cd "$KIT" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256
    shasum -a 256 "$IDENTITY"
    [[ -z "$SECRETS" ]] || shasum -a 256 "$SECRETS"
    [[ ${#PUBKEY[@]} -eq 0 ]] || shasum -a 256 "${PUBKEY[1]}"
); }
KIT_BEFORE=$(kit_hashes)

cleanup() {
    local code=$?
    trap - EXIT INT TERM HUP
    "$HERE/cleanup_dr.sh" "$PROJECT" >/dev/null 2>&1 || true
    rm -rf "$WORK"
    [[ ! -e "$WORK" ]] && echo "[real-drill] decrypted data removed" >&2
    if [[ "$(kit_hashes)" == "$KIT_BEFORE" ]]; then
        echo "[real-drill] recovery kit unchanged (SHA-256 of every file)" >&2
    else
        echo "[real-drill] WARNING: recovery kit files changed during the drill" >&2
        code=1
    fi
    exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
mkdir -p -m 700 "$REPORTS"
START=$(date +%s)
SECRETS_ARGS=()
[[ -z "$SECRETS" ]] || SECRETS_ARGS=(--secrets "$SECRETS")
"$HERE/prepare_kit_restore.sh" --kit "$KIT" --identity "$IDENTITY" --work "$WORK" \
    ${SECRETS_ARGS[@]+"${SECRETS_ARGS[@]}"} \
    ${PLAIN[@]+"${PLAIN[@]}"} ${SOURCE_GIT[@]+"${SOURCE_GIT[@]}"} \
    ${FRESH[@]+"${FRESH[@]}"} ${PUBKEY[@]+"${PUBKEY[@]}"}
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
