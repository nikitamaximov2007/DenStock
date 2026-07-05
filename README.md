# DenisStock — складской учёт запчастей

Веб-система складского учёта запчастей для ремонта автомобилей, снегоходов,
квадроциклов, катеров и яхт. Заказчик — Денис.

> Главная задача: во время звонка клиента за несколько секунд найти нужную
> запчасть и увидеть её количество, стоимость и точное местоположение на складе.

Стек: **Django + PostgreSQL**, запуск через **Docker Compose** (Caddy как reverse-proxy).
Подробности — в [docs/design/](docs/design/README.md).

## Требования

- Docker Desktop (Windows/Mac) или Docker Engine + Compose (Linux).

## Быстрый старт (Docker)

```bash
cp .env.example .env          # заполнить секреты (ключ, пароли)
docker compose up -d --build  # поднять db + web + proxy
```

Открыть в браузере: <http://localhost>.
В Windows можно просто запустить ярлык **`Старт.bat`**.

Первичный администратор создаётся автоматически из переменных `DJANGO_SUPERUSER_*`
в `.env` при первом запуске. Создать пользователя вручную:

```bash
docker compose exec web python manage.py createsuperuser
```

## Запуск без Docker (разработка/тесты, SQLite)

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install ".[dev]"
python manage.py migrate
python manage.py runserver                          # DEBUG=True → /media/ отдаётся Django
pytest
```

## Команды эксплуатации

```bash
# Запуск / остановка
docker compose up -d --build
docker compose down

# Миграции (в Docker применяются автоматически в entrypoint)
docker compose exec web python manage.py migrate

# Проверка готовности к эксплуатации (БД, media/backup writability, pg_dump)
docker compose exec web python manage.py ops_check

# Здоровье приложения (для мониторинга): GET /healthz/ → {"status":"ok","db":"ok"}
```

### Резервное копирование

Бэкапы складываются в `backups/<ДАТА_ВРЕМЯ>/` (видны на хосте через bind-mount).

```bash
# Полный бэкап: БД + media + manifest.json
docker compose exec web python manage.py backup_all
# Хранить только N последних бэкапов
docker compose exec web python manage.py backup_all --keep-last 7

# По отдельности
docker compose exec web python manage.py backup_db
docker compose exec web python manage.py backup_media
```

В каталоге рана: `db.dump` (PostgreSQL, формат `pg_dump -Fc`) или `db.sqlite3` (dev),
`media.tar.gz` и `manifest.json` (дата, движок, файлы, версия/коммит).

### Восстановление (ОПАСНО — перезапишет данные)

Restore требует явного флага `--yes`; без него команда откажется работать.

```bash
docker compose exec web python manage.py restore_db   backups/2026-06-30_12-00-00/db.dump --yes
docker compose exec web python manage.py restore_media backups/2026-06-30_12-00-00/media.tar.gz --yes
```

Ручной сценарий для PostgreSQL (если нужно вне команды):

```bash
docker compose exec -e PGPASSWORD=$POSTGRES_PASSWORD db \
  pg_restore --clean --if-exists --no-owner -U denstock -d denstock < db.dump
```

## Где лежат данные

- **Фото деталей/экземпляров (media):** `mediafiles/` (в Docker — именованный том `media`,
  переживает пересборку; Caddy отдаёт их по `/media/*`).
- **База данных:** том `pgdata` (PostgreSQL) или `db.sqlite3` (dev).
- **Бэкапы:** `backups/` (bind-mount на хост).

## Что НЕ коммитить

`.env`, `mediafiles/`, `backups/`, `db.sqlite3` — уже в `.gitignore`. Бэкапы и media
содержат коммерческие/персональные данные; храните копии бэкапов вне рабочей машины.

## Как обновлять проект

```bash
git pull
docker compose up -d --build   # пересборка; миграции применятся в entrypoint
docker compose exec web python manage.py ops_check
```

Перед обновлением рекомендуется `backup_all`.

## Troubleshooting

- **Фото не отображаются** — проверьте, что в `docker-compose.yml` есть том `media`
  (web и proxy), а в `Caddyfile` — маршрут `/media/*`; пересоберите: `docker compose up -d --build`.
- **БД не поднимается** — `docker compose logs db`; проверьте `POSTGRES_*` и `DATABASE_URL` в `.env`.
- **`pg_dump`/`pg_restore` не найдены** — образ должен содержать `postgresql-client`
  (добавлен в `docker/Dockerfile`); пересоберите образ.
- **Порт 80 занят** — измените публикацию портов сервиса `proxy` или освободите порт.
- **Забыли пароль администратора** — `docker compose exec web python manage.py changepassword <user>`.
- **Проверка состояния** — `docker compose exec web python manage.py ops_check`.

## Документация для пользователей

- [Инструкция пользователя](docs/user-guide/denstock-user-manual.md): все разделы, сценарии, ошибки, глоссарий.
- [Быстрый старт](docs/user-guide/quick-start.md): запуск склада с нуля за 8 шагов.
- [Чеклист запуска](docs/user-guide/launch-checklist.md): что проверить перед реальной работой.
- [База знаний для ChatGPT](docs/user-guide/denstock-chatgpt-context.md): файл для загрузки в ChatGPT-ассистента.

## Production / Operations docs

- [Production deploy runbook](docs/operations/production-deploy-runbook.md) — пошаговый деплой на VPS.
- [Post-deploy checklist](docs/operations/post-deploy-checklist.md) — что проверить после деплоя.
- [Restore runbook](docs/operations/restore-runbook.md): восстановление из бэкапа (CLI + защищённый веб-restore).
- [Scheduled + offsite backups](docs/operations/scheduled-offsite-backups.md) — cron/systemd + rclone.
- [Backup UI](docs/operations/backups-ui.md) — раздел «Бэкапы» в интерфейсе.
- [Инцидент: pg_restore transaction_timeout](docs/operations/incidents/2026-07-02-pg-restore-transaction-timeout.md)
- [План 37: совместимость версий PostgreSQL](docs/plans/37-postgres-backup-restore-version-compatibility.md)

## Структура проекта

```
config/         — Django-проект (настройки base/dev/prod/test, urls, wsgi/asgi)
apps/           — складские домены (catalog, inventory, sales, reports, labels …)
apps/operations — эксплуатация: backup/restore/ops_check (без моделей)
templates/      — шаблоны и партиалы
docker/         — Dockerfile, entrypoint, Caddy
docs/           — требования (ТЗ), дизайн, планы реализации (по слоям)
tests/          — pytest
```

## Статус

Дорожная карта реализована по вертикальным слоям (см.
[docs/design/05-roadmap.md](docs/design/05-roadmap.md)): склад, отчёты, печать
этикеток, фотографии деталей и — на финальном слое — резервное копирование и
эксплуатация. Проект готов к локальному использованию.

Здоровье приложения: `GET /healthz/` → `{"status":"ok","db":"ok"}`.
