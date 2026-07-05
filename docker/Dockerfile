# DenStock — образ приложения (Слой 1: только Python; сборка Tailwind добавится
# на UI-слоях). Один и тот же образ работает локально и на VPS.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

# Клиент PostgreSQL (pg_dump/pg_restore) для команд резервного копирования (Слой 25).
# ВАЖНО (план 37): клиент строго той же major-версии, что сервер (postgres:16 в
# docker-compose). Unversioned postgresql-client в Debian trixie = 17, а pg_dump 17
# пишет в дамп "SET transaction_timeout", которого PostgreSQL 16 не понимает
# (инцидент 2026-07-02). Ставим postgresql-client-16 из официального PGDG-репозитория.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Сначала зависимости — для кэширования слоёв.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Затем исходный код.
COPY . .

# Миграции, сборка статики и создание администратора выполняются в entrypoint
# при запуске, когда переменные окружения уже доступны.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
