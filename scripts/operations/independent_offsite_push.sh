#!/usr/bin/env bash
# Push the newest VERIFIED backup run to an INDEPENDENT provider, encrypted.
#
# Survives loss of the production VPS and of the primary Yandex bucket at the
# same time: the copy lives at another provider, is encrypted on this host to
# the owner's age RECIPIENT (public key; the identity never lives here), and is
# read back before it counts. The job never deletes anything: its credentials
# should allow only list/get/put, retention belongs to the provider's lifecycle
# rules or to a separate job with separate credentials.
#
# The rclone remote MUST set `no_check_bucket = true`: with list/get/put-only
# keys rclone would otherwise try CreateBucket first and every upload fails 403.
#
# Config (root, 0600): /etc/denstock/independent-offsite.env
#   INDEP_TARGET=<rclone remote:bucket/path>      INDEP_RCLONE_CONFIG=<file>
#   INDEP_RECIPIENT_FILE=<age recipient file>     BACKUP_ROOT=/opt/denstock/backups
#   INDEP_STATUS_FILE=<json>                      (optional) INDEP_RUN=<run id>
set -euo pipefail
umask 077
CONFIG="${INDEP_CONFIG:-/etc/denstock/independent-offsite.env}"
if [[ -f "$CONFIG" ]]; then set -a; . "$CONFIG"; set +a; fi
: "${INDEP_TARGET:?}" "${INDEP_RCLONE_CONFIG:?}" "${INDEP_RECIPIENT_FILE:?}" "${BACKUP_ROOT:?}"
STATUS_FILE="${INDEP_STATUS_FILE:-$BACKUP_ROOT/independent_status.json}"
LOCK="${INDEP_LOCK_FILE:-/run/lock/denstock-independent-offsite.lock}"
# Bounded retries: an unreachable provider ends the run as "failed" in minutes,
# not hours. INDEP_RCLONE_FLAGS replaces the defaults (tests use shorter ones).
read -r -a RCLONE_FLAGS <<< "${INDEP_RCLONE_FLAGS:---contimeout 30s --timeout 10m --retries 3 --low-level-retries 5}"
RCLONE=(rclone --config "$INDEP_RCLONE_CONFIG" "${RCLONE_FLAGS[@]}")
RUN="" STATE="failed" MESSAGE="Запуск прерван до завершения"

status() {
    STATE_V="$1" MESSAGE_V="$2" RUN_V="$RUN" TARGET_V="$INDEP_TARGET" PATH_V="$STATUS_FILE" \
        python3 - <<'PY'
import json, os
from datetime import datetime, timezone
from pathlib import Path
path = Path(os.environ["PATH_V"])
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps({
    "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "status": os.environ["STATE_V"], "message": os.environ["MESSAGE_V"],
    "run": os.environ["RUN_V"], "target": os.environ["TARGET_V"]}, ensure_ascii=False) + "\n")
tmp.replace(path)
PY
}
WORK=""
# The exit code is captured FIRST: any command run before it would replace $?,
# and a failed push must never end with status 0.
finish() {
    local code=$?
    [[ -n "$WORK" ]] && rm -rf "$WORK"
    [[ "$STATE" == running ]] && status failed "$MESSAGE" || true
    exit "$code"
}
trap finish EXIT
fail() { STATE=failed; status failed "$1"; echo "[independent] $1" >&2; exit 1; }

command -v flock >/dev/null || { echo "[independent] flock is not installed" >&2; trap - EXIT; exit 1; }
exec 9>"$LOCK"
flock -n 9 || { echo "[independent] another push is running" >&2; trap - EXIT; exit 75; }
STATE=running; status running "Шифрование и выгрузка выполняются"

RECIPIENT="$(tr -d '[:space:]' < "$INDEP_RECIPIENT_FILE")"
[[ "$RECIPIENT" =~ ^age1[0-9a-z]{58}$ ]] || fail "Получатель age некорректен"
command -v age >/dev/null || fail "age не установлен"

# Newest run whose manifest says verified (or the explicitly requested one).
RUN="${INDEP_RUN:-$(python3 - "$BACKUP_ROOT" <<'PY'
import json, re, sys
from pathlib import Path
root = Path(sys.argv[1])
runs = sorted(p for p in root.iterdir()
              if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", p.name))
for run in reversed(runs):
    try:
        if json.loads((run / "manifest.json").read_text()).get("verification_status") == "verified":
            print(run.name); break
    except (OSError, ValueError):
        continue
PY
)}"
[[ "$RUN" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]] || fail "Нет проверенной локальной копии"
OBJECT="$RUN.signed-backup.tar.age"
SIDECAR="$OBJECT.json"

EXISTING="$("${RCLONE[@]}" lsf "$INDEP_TARGET" --files-only 2>/dev/null)" ||
    fail "Независимое хранилище недоступно"
if printf '%s\n' "$EXISTING" | grep -Fxq "$SIDECAR"; then
    STATE=ok; status ok "Копия $RUN уже выгружена и подтверждена ранее"; exit 0
fi
if printf '%s\n' "$EXISTING" | grep -Fxq "$OBJECT"; then
    # A previous attempt was cut before its sidecar: never overwrite, report.
    fail "Есть неподтверждённый объект $OBJECT без подтверждения; нужна ручная проверка"
fi

WORK="$(mktemp -d)"
mkfifo "$WORK/cipher"
# Hash the exact ciphertext while it streams to the provider; plaintext never
# touches the disk and never leaves the host.
( sha256sum < "$WORK/cipher" | cut -c1-64 > "$WORK/cipher.sha256" ) &
HASHER=$!
tar -C "$BACKUP_ROOT" -cf - "$RUN" | age -r "$RECIPIENT" | tee "$WORK/cipher" |
    "${RCLONE[@]}" rcat "$INDEP_TARGET/$OBJECT" || fail "Выгрузка $OBJECT не завершена"
wait "$HASHER"
LOCAL_SHA="$(cat "$WORK/cipher.sha256")"

REMOTE_SHA="$("${RCLONE[@]}" cat "$INDEP_TARGET/$OBJECT" | sha256sum | cut -c1-64)" ||
    fail "Чтение назад $OBJECT не удалось"
[[ "$REMOTE_SHA" == "$LOCAL_SHA" ]] || fail "Чтение назад $OBJECT не совпало"

python3 - "$BACKUP_ROOT/$RUN/manifest.json" "$RUN" "$LOCAL_SHA" "$RECIPIENT" > "$WORK/sidecar.json" <<'PY'
import hashlib, json, sys
manifest_path, run, cipher_sha, recipient = sys.argv[1:5]
raw = open(manifest_path, "rb").read()
m = json.loads(raw)
print(json.dumps({
    "run": run, "created_at": m.get("created_at"), "app_commit": m.get("app_commit"),
    "private_media_status": m.get("private_media_status"),
    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    "ciphertext_sha256": cipher_sha, "age_recipient": recipient,
}, indent=1))
PY
"${RCLONE[@]}" rcat "$INDEP_TARGET/$SIDECAR" < "$WORK/sidecar.json" ||
    fail "Подтверждение $SIDECAR не записано"
STATE=ok; status ok "Копия $RUN зашифрована, выгружена и прочитана назад"
echo "[independent] $RUN pushed and verified"
