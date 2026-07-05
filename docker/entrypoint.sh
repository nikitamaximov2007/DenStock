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

echo "[entrypoint] Применение миграций…"
python manage.py migrate --noinput

echo "[entrypoint] Сборка статики…"
python manage.py collectstatic --noinput

# Создание первичного администратора из переменных окружения (если заданы).
if [[ -n "${DJANGO_SUPERUSER_USERNAME:-}" && -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
  echo "[entrypoint] Проверка/создание администратора ${DJANGO_SUPERUSER_USERNAME}…"
  python manage.py createsuperuser --noinput || true
fi

echo "[entrypoint] Запуск: $*"
exec "$@"
