# DenStock — образ приложения (Слой 1: только Python; сборка Tailwind добавится
# на UI-слоях). Один и тот же образ работает локально и на VPS.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

# Клиент PostgreSQL (pg_dump/pg_restore) для команд резервного копирования (Слой 25).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
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
