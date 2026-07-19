# DenStock — образ приложения (Слой 1: только Python; сборка Tailwind добавится
# на UI-слоях). Один и тот же образ работает локально и на VPS.
FROM python:3.12-slim

ARG DENSTOCK_WEB_UID=2001
ARG DENSTOCK_WEB_GID=2001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

# Клиенты PostgreSQL для резервного копирования (Слой 25, план 37, hotfix 2).
# Ставим ОБА из официального PGDG-репозитория:
#  - postgresql-client-16: pg_dump той же major-версии, что сервер postgres:16
#    (дампы без "SET transaction_timeout"; им же идёт restore);
#  - postgresql-client-17: только чтение СТАРЫХ архивов pg_dump 17 (формат 1.16),
#    которые pg_restore 16 не открывает ("unsupported version (1.16) in file header").
# Код обращается к клиентам по явным путям /usr/lib/postgresql/<v>/bin/*.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 postgresql-client-17 \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Сначала зависимости — для кэширования слоёв.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Затем исходный код.
COPY . .

# The production AI override runs Django as this fixed non-root identity. Code
# remains root-owned; only application data directories are writable.
RUN groupadd --gid "${DENSTOCK_WEB_GID}" denstock-web \
    && useradd --uid "${DENSTOCK_WEB_UID}" --gid denstock-web \
        --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin denstock-web \
    && install -d -o denstock-web -g denstock-web -m 0755 \
        /app/staticfiles /app/mediafiles /app/private_media /app/backups

# Миграции, сборка статики и создание администратора выполняются в entrypoint
# при запуске, когда переменные окружения уже доступны.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
