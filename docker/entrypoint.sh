#!/usr/bin/env bash
# Entrypoint DenisStock: дождаться БД, применить миграции, собрать статику,
# при необходимости создать администратора — затем запустить приложение.
set -euo pipefail

echo "[entrypoint] Ожидание базы данных…"
python - <<'PY'
import os, time
import psycopg

dsn = os.environ.get("DATABASE_URL", "")
for attempt in range(60):
    try:
        psycopg.connect(dsn, connect_timeout=2).close()
        print("[entrypoint] База данных доступна.")
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("[entrypoint] Не дождались базы данных.")
PY

if [[ "${DENSTOCK_MODE:-}" == "emergency-local" ]]; then
  emergency_control_db="${DENSTOCK_EMERGENCY_DB_PREFIX:-denstock_emergency_}control"
  if [[ "${DENSTOCK_EMERGENCY_DATABASE_NAME:-}" == "${emergency_control_db}" \
        && "${POSTGRES_DB:-}" == "${emergency_control_db}" ]]; then
    echo "[entrypoint] Применение миграций к служебной emergency control DB…"
    python manage.py migrate --noinput
  else
    echo "[entrypoint] Проверка миграций emergency standby без изменения БД…"
    python manage.py migrate --check
  fi
else
  echo "[entrypoint] Применение миграций…"
  python manage.py migrate --noinput
fi

echo "[entrypoint] Сборка статики…"
python manage.py collectstatic --noinput

# Создание первичного администратора из переменных окружения (если заданы).
if [[ "${DENSTOCK_MODE:-}" != "emergency-local" \
      && -n "${DJANGO_SUPERUSER_USERNAME:-}" \
      && -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
  echo "[entrypoint] Проверка/создание администратора ${DJANGO_SUPERUSER_USERNAME}…"
  python manage.py createsuperuser --noinput || true
fi

echo "[entrypoint] Запуск: $*"
exec "$@"
