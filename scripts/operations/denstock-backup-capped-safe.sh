#!/usr/bin/env bash
# Candidate replacement for the production-only denstock-backup-capped wrapper.
# Never installs itself and never runs against cloud storage during qualification.
# A new signed generation is uploaded and read back BEFORE any old run is removed.
set -euo pipefail

PROJECT_ROOT="${DENSTOCK_PROJECT_ROOT:-/opt/denstock}"
LOCK_FILE="${DENSTOCK_BACKUP_LOCK_FILE:-/run/lock/denstock-backup.lock}"
cd "$PROJECT_ROOT"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "backup already running" >&2; exit 75; }

BACKUP_KEEP_LAST=14
BACKUP_OFFSITE_ENABLED=false
BACKUP_OFFSITE_METHOD=rclone
BACKUP_OFFSITE_TARGET=""
BACKUP_REMOTE_SOFT_LIMIT_BYTES=838860800
BACKUP_STATUS_FILE=backups/offsite_status.json
BACKUP_WEB_SERVICE=web
if [[ -f .env.backup ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env.backup
    set +a
fi

RUN_ID=""
RUN_PATH=""
TERMINAL_STATUS_WRITTEN=0
status() {
    local state="$1" message="$2" uploaded="${3:-}" exit_code="${4:-0}"
    STATUS_PATH="$BACKUP_STATUS_FILE" STATE="$state" MESSAGE="$message" \
    RUN_ID="$RUN_ID" RUN_PATH="$RUN_PATH" UPLOADED="$uploaded" \
    EXIT_CODE="$exit_code" TARGET="$BACKUP_OFFSITE_TARGET" \
    ENABLED="$BACKUP_OFFSITE_ENABLED" python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["STATUS_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "checked_at": datetime.now(timezone.utc).isoformat(),
    "local_backup_run": os.environ["RUN_ID"],
    "local_backup_path": os.environ["RUN_PATH"],
    "offsite_enabled": os.environ["ENABLED"].lower() == "true",
    "method": "rclone",
    "target": os.environ["TARGET"],
    "status": os.environ["STATE"],
    "message": os.environ["MESSAGE"],
    "uploaded_run": os.environ["UPLOADED"],
    "exit_code": int(os.environ["EXIT_CODE"]),
}
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
temporary.replace(path)
PY
    if [[ "$state" != running ]]; then
        TERMINAL_STATUS_WRITTEN=1
    fi
}
fail() {
    status failed "$1" "" 1
    echo "[denstock-backup] $1" >&2
    exit 1
}
size_bytes() {
    local result
    result="$(rclone size --s3-versions "$1" --json)" || return 1
    printf '%s' "$result" | python3 -c '
import json, sys
value = json.load(sys.stdin)["bytes"]
if type(value) is not int or value < 0:
    raise SystemExit(2)
print(value)
'
}
remote_runs() {
    local listing
    listing="$(rclone lsf "$BACKUP_OFFSITE_TARGET" --dirs-only --max-depth 1)" || return 1
    while IFS= read -r item; do
        [[ -z "$item" ]] && continue
        [[ "$item" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}/$ ]] ||
            return 1
        printf '%s\n' "${item%/}"
    done <<< "$listing" | LC_ALL=C sort
}
run_bytes() {
    RUN_PATH_VALUE="$RUN_PATH" python3 - <<'PY'
import os
from pathlib import Path
print(sum(p.stat().st_size for p in Path(os.environ["RUN_PATH_VALUE"]).rglob("*")
          if p.is_file()))
PY
}

# A run killed by a signal (systemd stop, reboot) or by an unexpected error
# must not leave yesterday's "ok" in the status file.
on_exit() {
    local code=$?
    if [[ "$TERMINAL_STATUS_WRITTEN" != 1 ]]; then
        status failed "Запуск прерван до завершения; новая копия не подтверждена" "" \
            "$(( code == 0 ? 1 : code ))" || true
    fi
}
trap on_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
status running "Создание и выгрузка копии выполняются" "" 0 ||
    echo "[denstock-backup] status-файл недоступен; продолжаю без него" >&2

BACKUP_OUTPUT="$(docker compose exec -T "$BACKUP_WEB_SERVICE" \
    python manage.py backup_all --trigger automatic --keep-last "$BACKUP_KEEP_LAST")" ||
    fail "Локальный backup завершился ошибкой; offsite не изменён"
RUN_ID="$(printf '%s\n' "$BACKUP_OUTPUT" | \
    sed -nE 's#^.*backups/([0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2})[^0-9]*$#\1#p' | tail -1)"
[[ "$RUN_ID" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]] ||
    fail "Не удалось определить точный каталог нового backup"
RUN_PATH="backups/$RUN_ID"
[[ -d "$RUN_PATH" && -f "$RUN_PATH/manifest.json" ]] ||
    fail "Новый локальный backup отсутствует или неполон"
docker compose exec -T "$BACKUP_WEB_SERVICE" \
    python manage.py verify_backup "$RUN_ID" >/dev/null ||
    fail "Локальный backup не прошёл проверку подписи и целостности"

if [[ "$BACKUP_OFFSITE_ENABLED" != true ]]; then
    status not_configured "Offsite отключён; локальная копия сохранена"
    exit 0
fi
[[ "$BACKUP_OFFSITE_METHOD" == rclone ]] || fail "Неподдерживаемый offsite method"
[[ "$BACKUP_OFFSITE_TARGET" =~ ^[A-Za-z0-9_-]+:[A-Za-z0-9][A-Za-z0-9/_.-]*$ ]] ||
    fail "Offsite target отсутствует или небезопасен"
[[ "$BACKUP_OFFSITE_TARGET" != *".."* ]] || fail "Offsite target содержит небезопасный путь"
[[ "$BACKUP_REMOTE_SOFT_LIMIT_BYTES" =~ ^[0-9]+$ ]] || fail "Soft limit некорректен"
command -v rclone >/dev/null 2>&1 || fail "rclone недоступен"

LOCAL_BYTES="$(run_bytes)" || fail "Размер локальной копии неизвестен"
REMOTE_BEFORE="$(size_bytes "$BACKUP_OFFSITE_TARGET")" ||
    fail "Размер удалённого хранилища неизвестен; offsite не изменён"
BEFORE_RUNS="$(remote_runs)" || fail "Список удалённых поколений недоступен"
if printf '%s\n' "$BEFORE_RUNS" | grep -Fxq "$RUN_ID"; then
    fail "Удалённое поколение с этим ID уже существует; перезапись запрещена"
fi
if (( REMOTE_BEFORE + LOCAL_BYTES > BACKUP_REMOTE_SOFT_LIMIT_BYTES )); then
    fail "Недостаточно подтверждённого места для новой копии; старые не удалены"
fi

DEST="$BACKUP_OFFSITE_TARGET/$RUN_ID"
rclone copy "$RUN_PATH" "$DEST" || fail "Upload не завершён; старые копии сохранены"
rclone check --download "$RUN_PATH" "$DEST" ||
    fail "Read-back новой копии не совпал; старые копии сохранены"
# The read-back proves remote == local bytes. Re-verify the local signature and
# hashes now, so the remote copy is equal to bytes that still verify, not to a
# local copy that changed after the first verification.
docker compose exec -T "$BACKUP_WEB_SERVICE" \
    python manage.py verify_backup "$RUN_ID" >/dev/null ||
    fail "Локальная копия изменилась после первой проверки; ротация не выполнялась"
AFTER_RUNS="$(remote_runs)" || fail "После upload список удалённых поколений недоступен"
if ! printf '%s\n' "$AFTER_RUNS" | grep -Fxq "$RUN_ID"; then
    fail "Новое поколение не видно в удалённом списке"
fi
EXPECTED_RUNS="$(printf '%s\n%s\n' "$BEFORE_RUNS" "$RUN_ID" | sed '/^$/d' | LC_ALL=C sort)"
if [[ "$AFTER_RUNS" != "$EXPECTED_RUNS" ]]; then
    fail "Удалённый список изменился параллельно; ротация не выполнялась"
fi
REMOTE_AFTER="$(size_bytes "$BACKUP_OFFSITE_TARGET")" ||
    fail "После upload размер удалённого хранилища неизвестен"
if (( REMOTE_AFTER > BACKUP_REMOTE_SOFT_LIMIT_BYTES )); then
    fail "После upload учтённый размер превысил soft limit; новую копию не удалять"
fi

# Preserve the new verified generation and the most recent PROVEN older one.
# An older generation is proven only when its local signed copy still verifies
# and matches remote byte for byte. Walking back from the newest: a copy whose
# local twin verifies but whose remote bytes differ is a broken (interrupted)
# upload, not a recovery point; a copy without a verifiable local twin cannot be
# judged, so rotation stops there without deleting anything.
PREVIOUS=""
BROKEN=()
while IFS= read -r CANDIDATE; do
    [[ -z "$CANDIDATE" ]] && continue
    if [[ ! -d "backups/$CANDIDATE" ]] || \
        ! docker compose exec -T "$BACKUP_WEB_SERVICE" \
            python manage.py verify_backup "$CANDIDATE" >/dev/null </dev/null; then
        status degraded "Новая копия проверена; предыдущая не подтверждена, ротация остановлена" \
            "$RUN_ID" 1
        exit 1
    fi
    if rclone check --download "backups/$CANDIDATE" "$BACKUP_OFFSITE_TARGET/$CANDIDATE" \
        </dev/null; then
        PREVIOUS="$CANDIDATE"
        break
    fi
    BROKEN+=("$CANDIDATE")
    if (( ${#BROKEN[@]} >= 3 )); then
        status degraded "Несколько удалённых копий подряд повреждены; ротация остановлена" \
            "$RUN_ID" 1
        exit 1
    fi
done < <(printf '%s\n' "$BEFORE_RUNS" | sed '/^$/d' | LC_ALL=C sort -r)
if [[ -n "$BEFORE_RUNS" && -z "$PREVIOUS" ]]; then
    status degraded "Новая копия проверена; предыдущая не подтверждена, ротация остановлена" \
        "$RUN_ID" 1
    exit 1
fi

# Broken copies newer than the proven previous one are interrupted uploads.
# Two proven generations (new and previous) exist, so removing them is safe.
for BAD in ${BROKEN[@]+"${BROKEN[@]}"}; do
    [[ "$BAD" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]] ||
        fail "Небезопасное имя поколения; ротация остановлена"
    [[ "$BAD" != "$RUN_ID" && "$BAD" != "$PREVIOUS" ]] ||
        fail "Внутренняя ошибка выбора повреждённой копии; ротация остановлена"
    rclone purge "$BACKUP_OFFSITE_TARGET/$BAD" ||
        fail "Не удалось удалить повреждённую копию $BAD; остальные копии сохранены"
    rclone backend cleanup-hidden "$BACKUP_OFFSITE_TARGET/$BAD" ||
        fail "Не удалось очистить версии повреждённой копии $BAD; остальные сохранены"
done
if (( ${#BROKEN[@]} > 0 )); then
    AFTER_RUNS="$(remote_runs)" || fail "После удаления повреждённых копий список неизвестен"
    REMOTE_AFTER="$(size_bytes "$BACKUP_OFFSITE_TARGET")" ||
        fail "После удаления повреждённых копий размер неизвестен"
fi

# Keep enough headroom for another generation of the same measured size.
# Never delete before the new upload/read-back and never touch other namespaces.
if [[ -n "$PREVIOUS" ]]; then
    while (( REMOTE_AFTER + LOCAL_BYTES > BACKUP_REMOTE_SOFT_LIMIT_BYTES )); do
        OLDEST="$(printf '%s\n' "$AFTER_RUNS" | head -1)"
        if [[ -z "$OLDEST" || "$OLDEST" == "$RUN_ID" || "$OLDEST" == "$PREVIOUS" ]]; then
            status degraded "Резерв места недостаточен; две проверенные копии сохранены" \
                "$RUN_ID" 1
            exit 1
        fi
        [[ "$OLDEST" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]] ||
            fail "Небезопасное имя поколения; ротация остановлена"
        BEFORE_DELETE="$REMOTE_AFTER"
        rclone purge "$BACKUP_OFFSITE_TARGET/$OLDEST" ||
            fail "Targeted purge завершился ошибкой; остальные копии сохранены"
        rclone backend cleanup-hidden "$BACKUP_OFFSITE_TARGET/$OLDEST" ||
            fail "Targeted cleanup версий завершился ошибкой; остальные копии сохранены"
        REMOTE_AFTER="$(size_bytes "$BACKUP_OFFSITE_TARGET")" ||
            fail "После ротации размер удалённого хранилища неизвестен"
        (( REMOTE_AFTER < BEFORE_DELETE )) ||
            fail "Удаление не освободило учтённое место; ротация остановлена"
        AFTER_RUNS="$(remote_runs)" || fail "После ротации список удалённых копий неизвестен"
        printf '%s\n' "$AFTER_RUNS" | grep -Fxq "$RUN_ID" ||
            fail "Новое проверенное поколение исчезло; ротация остановлена"
    done
fi

# Both survivors were already read back in full above. After rotation a hash
# comparison (no download) proves they were not touched, at no egress cost.
if ! rclone check "$RUN_PATH" "$DEST"; then
    fail "После ротации новое поколение не совпадает по контрольным суммам"
fi
if [[ -n "$PREVIOUS" ]] && \
    ! rclone check "backups/$PREVIOUS" "$BACKUP_OFFSITE_TARGET/$PREVIOUS"; then
    fail "После ротации предыдущее поколение не совпадает по контрольным суммам"
fi
status ok "Новая копия проверена чтением; учтённый размер $REMOTE_AFTER байт" "$RUN_ID"
