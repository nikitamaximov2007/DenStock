# DenStock — складской учёт запчастей

Веб-система складского учёта запчастей для ремонта автомобилей, снегоходов,
квадроциклов, катеров и яхт. Заказчик — Денис.

> Главная задача: во время звонка клиента за несколько секунд найти нужную
> запчасть и увидеть её количество, стоимость и точное местоположение на складе.

Стек: **Django + HTMX + Tailwind + PostgreSQL**, запуск через **Docker Compose**.
Подробности — в [docs/design/](docs/design/README.md).

## Требования

- Docker Desktop (Windows/Mac) или Docker Engine + Compose (Linux).

## Быстрый старт

```bash
cp .env.example .env          # заполнить секреты (ключ, пароли)
docker compose up -d --build  # поднять db + web + proxy
```

Открыть в браузере: <http://localhost>.
В Windows можно просто запустить ярлык **`Старт.bat`**.

Первичный администратор создаётся автоматически из переменных
`DJANGO_SUPERUSER_*` в `.env` при первом запуске.

## Команды

```bash
# Запуск / остановка
docker compose up -d --build
docker compose down

# Миграции
docker compose exec web python manage.py migrate
docker compose exec web python manage.py makemigrations

# Создать пользователя вручную
docker compose exec web python manage.py createsuperuser

# Тесты и линтеры (нужны dev-зависимости: pip install ".[dev]")
docker compose exec web pytest
docker compose exec web ruff check .
docker compose exec web djlint templates --check
```

Локальный запуск без Docker (для разработки/тестов, на SQLite):

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install ".[dev]"
python manage.py migrate
python manage.py runserver
pytest
```

## Структура проекта

```
config/        — Django-проект (настройки base/dev/prod/test, urls, wsgi/asgi)
apps/accounts  — кастомная модель пользователя
apps/core      — layout, главная, healthcheck
templates/     — базовые шаблоны и партиалы
static/        — статика (скелетный app.css; Tailwind — на UI-слоях)
docker/        — Dockerfile, entrypoint, Caddy
docs/          — требования (ТЗ), дизайн, планы реализации
tests/         — pytest
```

## Статус

Реализуется по вертикальным слоям (см.
[docs/design/05-roadmap.md](docs/design/05-roadmap.md)). Текущий слой: **1 —
каркас**. Резервное копирование и запуск «в один клик» — на более поздних слоях.

Здоровье приложения: `GET /healthz/` → `{"status":"ok","db":"ok"}`.
