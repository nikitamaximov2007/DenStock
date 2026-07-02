#!/usr/bin/env bash
# v1.1.8B — автоматический бэкап + опциональный offsite (host-level).
#
# Запуск вручную или по cron/systemd из любого места (скрипт сам переходит в корень
# проекта). Создаёт automatic-бэкап внутри контейнера web, при включённом offsite
# отправляет его через rclone и пишет backups/offsite_status.json для UI.
#
# НЕ делает restore. НЕ читает и НЕ отправляет .env (секреты). Секреты offsite — только
# в конфиге rclone на сервере, НЕ в этом скрипте и НЕ в Git.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

log() { echo "[backup_offsite] $*"; }

# --- Конфиг (без секретов) из .env.backup, если есть ---
BACKUP_KEEP_LAST=14
BACKUP_OFFSITE_ENABLED=false
BACKUP_OFFSITE_METHOD=rclone
BACKUP_OFFSITE_TARGET=""
BACKUP_STATUS_FILE="backups/offsite_status.json"
BACKUP_WEB_SERVICE="web"
if [ -f .env.backup ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env.backup; set +a
fi

if [ "${BACKUP_OFFSITE_ENABLED}" = "true" ]; then ENABLED=true; else ENABLED=false; fi
STATUS_FILE="${BACKUP_STATUS_FILE:-backups/offsite_status.json}"
mkdir -p "$(dirname "$STATUS_FILE")"

RUN_ID=""
RUN_PATH=""

# Записать статус для UI. Секреты сюда НЕ пишем (target — это имя rclone-remote, не ключ).
write_status() {
  local status="$1" message="$2" uploaded_run="${3:-}" exit_code="${4:-0}"
  cat > "$STATUS_FILE" <<EOF
{
  "checked_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "local_backup_run": "${RUN_ID}",
  "local_backup_path": "${RUN_PATH}",
  "offsite_enabled": ${ENABLED},
  "method": "${BACKUP_OFFSITE_METHOD}",
  "target": "${BACKUP_OFFSITE_TARGET}",
  "status": "${status}",
  "message": "${message}",
  "uploaded_run": "${uploaded_run}",
  "exit_code": ${exit_code}
}
EOF
  log "статус: ${status} → ${STATUS_FILE}"
}

# --- 1. Создать automatic-бэкап внутри контейнера web ---
if [ "$DRY_RUN" = "1" ]; then
  log "DRY-RUN: docker compose exec -T ${BACKUP_WEB_SERVICE} python manage.py backup_all --trigger automatic --keep-last ${BACKUP_KEEP_LAST}"
else
  log "создаю автоматический бэкап (keep-last=${BACKUP_KEEP_LAST})..."
  docker compose exec -T "${BACKUP_WEB_SERVICE}" \
    python manage.py backup_all --trigger automatic --keep-last "${BACKUP_KEEP_LAST}"
fi

# --- 2. Найти последний backup run (каталог backups/<timestamp>/) ---
RUN_PATH="$(ls -1d backups/*/ 2>/dev/null | sort | tail -1 || true)"
RUN_PATH="${RUN_PATH%/}"
if [ -z "$RUN_PATH" ]; then
  if [ "$DRY_RUN" = "1" ]; then log "DRY-RUN: локальных бэкапов нет — ок"; exit 0; fi
  write_status "failed" "Не найден созданный бэкап" "" 1
  log "ОШИБКА: бэкап не найден" >&2
  exit 1
fi
RUN_ID="$(basename "$RUN_PATH")"
log "последний бэкап: ${RUN_ID}"

# --- 3. Проверить manifest ---
if [ ! -f "${RUN_PATH}/manifest.json" ]; then
  write_status "failed" "manifest.json отсутствует в ${RUN_ID}" "" 1
  log "ОШИБКА: manifest отсутствует" >&2
  exit 1
fi

# --- 4. Offsite отключён → статус not_configured, exit 0 ---
if [ "$ENABLED" != "true" ]; then
  write_status "not_configured" "Offsite отключён (BACKUP_OFFSITE_ENABLED=false)" "" 0
  log "offsite отключён — только локальный бэкап"
  exit 0
fi

# --- 5. Offsite включён: проверки и отправка через rclone ---
if [ -z "$BACKUP_OFFSITE_TARGET" ]; then
  write_status "failed" "BACKUP_OFFSITE_TARGET не задан" "" 1
  log "ОШИБКА: target не задан" >&2
  exit 1
fi
if [ "$BACKUP_OFFSITE_METHOD" != "rclone" ]; then
  write_status "failed" "Неподдерживаемый метод: ${BACKUP_OFFSITE_METHOD}" "" 1
  exit 1
fi
if ! command -v rclone >/dev/null 2>&1; then
  write_status "failed" "rclone не установлен на сервере" "" 1
  log "ОШИБКА: rclone не найден" >&2
  exit 1
fi

DEST="${BACKUP_OFFSITE_TARGET}/${RUN_ID}"
if [ "$DRY_RUN" = "1" ]; then
  log "DRY-RUN: rclone copy ${RUN_PATH} ${DEST}"
  exit 0
fi

log "отправляю ${RUN_ID} в offsite (${BACKUP_OFFSITE_TARGET})..."
if rclone copy "$RUN_PATH" "$DEST"; then
  write_status "ok" "Отправлено в offsite" "$RUN_ID" 0
  log "offsite OK"
else
  rc=$?
  write_status "failed" "rclone copy завершился с ошибкой" "" "$rc"
  log "ОШИБКА offsite (rclone rc=${rc})" >&2
  exit 1
fi
