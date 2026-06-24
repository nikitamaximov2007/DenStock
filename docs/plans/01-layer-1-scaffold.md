# План реализации — Слой 1. Каркас проекта

**Статус:** на согласовании (2026-06-24) · **Код не пишется до утверждения.**

## Цель

Запускаемое **одной командой** Django-приложение на PostgreSQL в Docker, с
базовым складским layout, авторизацией (вход/выход), базовой главной страницей,
healthcheck-эндпоинтом, настроенными линтерами/форматтерами и проходящими
тестами. Это фундамент для всех последующих слоёв.

## Структура папок (после Слоя 1)

```
DenStock/
├─ docker-compose.yml
├─ .env.example                # шаблон переменных окружения
├─ .dockerignore
├─ pyproject.toml              # зависимости Python + конфиг ruff
├─ manage.py
├─ docker/
│  ├─ Dockerfile               # multi-stage: node(Tailwind) → python
│  ├─ entrypoint.sh            # ожидание БД, миграции, запуск
│  └─ caddy/Caddyfile          # reverse-proxy + статика (VPS-ready)
├─ config/                     # Django-проект
│  ├─ settings/{base,dev,prod}.py
│  ├─ urls.py · wsgi.py · asgi.py
├─ apps/
│  ├─ accounts/                # кастомная модель User
│  └─ core/                    # layout, главная, healthcheck
├─ templates/
│  ├─ base.html
│  ├─ registration/login.html
│  ├─ core/dashboard.html
│  └─ partials/{_status_block,_scan_field}.html
├─ assets/                     # исходники Tailwind (src.css, tailwind.config.js, package.json)
├─ static/                     # собранный css (генерируется)
├─ tests/                      # pytest
├─ docs/                       # уже есть
├─ Старт.bat / start.sh        # простой запуск (полный «один клик» — Слой 25)
└─ README.md
```

## Django-проект и приложения, создаваемые сразу

- **`config`** — проект (раздельные настройки `base/dev/prod`).
- **`apps.accounts`** — **кастомная модель `User`** (`AbstractUser` + `full_name`)
  ставится **сразу на Слое 1** (менять модель User позже крайне болезненно).
  Роли и права — Слой 2.
- **`apps.core`** — базовый layout, главная страница, healthcheck, общие
  партиалы (статус-блок, поле сканера-заглушка).

Остальные приложения (`catalog`, `suppliers`, `warehouse`, `procurement`,
`inventory`, `reservations`, `sales`, `operations`, `analytics`, `barcodes`,
`audit`) **не создаём заранее** — каждое появляется на своём слое
(вертикальный принцип). Полный список зафиксирован в `01-architecture.md`.

## Docker Compose

Три сервиса (тот же файл локально и на VPS):

| Сервис | Назначение |
|---|---|
| `db` | `postgres:16`, постоянный том `pgdata`, healthcheck `pg_isready` |
| `web` | Django + Gunicorn; ждёт healthy `db`, применяет миграции, отдаёт `:8000` |
| `proxy` | Caddy: проксирует на `web`, отдаёт статику; на VPS включает авто-HTTPS по домену |

Локально открывается на `http://localhost`. Другие устройства в сети — по адресу
складского ПК.

## PostgreSQL

`postgres:16`; имя БД/пользователь/пароль — из `.env`; постоянный том; доступ
через `DATABASE_URL`. Никаких ручных установок СУБД на хост — всё в контейнере.

## Настройки окружения (`.env.example`)

`DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`,
`DJANGO_SETTINGS_MODULE`, `POSTGRES_DB/USER/PASSWORD/HOST/PORT` (или
`DATABASE_URL`), `TIME_ZONE=Europe/Moscow`, `LANGUAGE_CODE=ru-ru`,
`DJANGO_SUPERUSER_*` (для первичного создания администратора).
Чтение настроек — через `django-environ`. Секреты не коммитятся (есть `.gitignore`).

## Базовый layout

`base.html` на Tailwind (собирается из исходников, без CDN — работает офлайн в
локальной сети):
- верхняя панель: название системы, **глобальное поле поиска** и **постоянное
  поле сканера** (визуальные заглушки на Слое 1), меню пользователя + «Выйти»;
- боковая навигация (на Слое 1 — статические пункты, роль-зависимость со Слоя 2);
- область контента;
- переиспользуемый **статус-блок** (цвет + текст + иконка: «Успешно» / «Ошибка» /
  «Требуется действие») — компонент для будущих экранов.
Язык интерфейса — русский.

## Авторизация

Штатная Django-аутентификация: экраны входа/выхода, `login.html`,
`LOGIN_REDIRECT_URL` → главная, `login_required` на защищённых страницах.
Сам/регистрации нет — пользователей создаёт администратор. Первичный
суперпользователь создаётся из `DJANGO_SUPERUSER_*` при первом запуске
(`entrypoint`) либо командой.

## Базовая главная страница

`dashboard` (под `login_required`): заглушки плиток (наполнятся на своих слоях) и
имя вошедшего пользователя. Это «скелет» главной панели из 4.1.

## Healthcheck

`GET /healthz/` → `200` с JSON `{status: ok, db: ok}` (проверяет соединение с БД).
Используется healthcheck'ом Docker и для контроля доступности.

## Линтеры/форматтеры

- **`ruff`** — линтинг и форматирование Python (конфиг в `pyproject.toml`).
- **`djlint`** — проверка/форматирование шаблонов.
- Опционально `pre-commit` для автозапуска перед коммитом.

## Тесты (pytest + pytest-django) — обязательные на Слое 1

1. Приложение и настройки загружаются; миграции применяются «с нуля».
2. `/healthz/` возвращает 200 и `db: ok`.
3. Аноним на защищённой странице → редирект на вход.
4. Вход с верными данными → главная.
5. Выход завершает сессию.

## Команды запуска

```bash
cp .env.example .env                 # заполнить секреты
docker compose up --build            # поднять db+web+proxy → http://localhost
docker compose exec web pytest       # тесты
docker compose exec web ruff check . # линтер
docker compose exec web python manage.py createsuperuser   # при необходимости
```

## Что должно быть в README (после Слоя 1)

- что за система (1 абзац) и ссылка на `docs/design`;
- требования (Docker Desktop);
- быстрый старт: `.env` → `docker compose up` → открыть `localhost` → войти;
- команды: тесты, линтер, миграции, создание пользователя;
- краткая структура проекта;
- примечание: бэкап/восстановление и «один клик» — на более поздних слоях.

## Критерии готовности Слоя 1

1. `docker compose up` поднимает `db` + `web` + `proxy`, приложение открывается в браузере.
2. Вход/выход работают; неавторизованный редиректится на вход.
3. Главная открывается под логином.
4. `/healthz/` возвращает 200 с проверкой БД.
5. `pytest` зелёный (≥ 5 тестов выше).
6. `ruff check` без ошибок.
7. Кастомная модель `User` на месте; миграции применяются с нуля.
8. По README проект запускается с чистой машины по шагам.

## Список файлов, которые будут созданы

- `docker-compose.yml`, `.env.example`, `.dockerignore`
- `docker/Dockerfile`, `docker/entrypoint.sh`, `docker/caddy/Caddyfile`
- `pyproject.toml`, `manage.py`
- `config/__init__.py`, `config/settings/{__init__,base,dev,prod}.py`,
  `config/urls.py`, `config/wsgi.py`, `config/asgi.py`
- `apps/__init__.py`
- `apps/accounts/{__init__,apps,models,admin}.py`, `apps/accounts/migrations/`
- `apps/core/{__init__,apps,views,urls}.py`, `apps/core/migrations/`
- `templates/base.html`, `templates/registration/login.html`,
  `templates/core/dashboard.html`,
  `templates/partials/_status_block.html`, `templates/partials/_scan_field.html`
- `assets/package.json`, `assets/tailwind.config.js`, `assets/src.css`
- `tests/__init__.py`, `tests/test_health.py`, `tests/test_auth.py`
- `Старт.bat`, `start.sh`
- `README.md` (обновление)

## Что будет закоммичено

Один коммит слоя:

```
Слой 1: каркас Django+Postgres+Docker, layout, авторизация
```

Содержит все файлы выше, проходящие тесты и чистый линтер. После коммита —
переход к плану Слоя 2 (пользователи, роли и права).
