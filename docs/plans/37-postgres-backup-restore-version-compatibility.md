# План 37 — Совместимость версий PostgreSQL для backup/restore

**Статус: РЕАЛИЗОВАНО (Layer 30 hotfix + hotfix 2, 2026-07-05).** Выбран
вариант A + D: пин `postgresql-client-16` в `docker/Dockerfile` (PGDG) + surface
предупреждений в `restore_db` (толерантность СТРОГО к известной ошибке
`transaction_timeout`; остальные ошибки фатальны). `verify_backup` предупреждает
заранее. Hotfix 2: pg_restore 16 не читает custom-архивы pg_dump 17
(формат 1.16, «unsupported version (1.16) in file header»), поэтому дампы
делаются явным `/usr/lib/postgresql/16/bin/pg_dump`, а restore при этой
единственной ошибке делает fallback на `/usr/lib/postgresql/17/bin/pg_restore`
(клиент 17 тоже стоит в образе). Тесты: `tests/test_restore_compat.py`.
Ниже: исходный план.

Устраняет technical
debt из [инцидента 2026-07-02](../operations/incidents/2026-07-02-pg-restore-transaction-timeout.md):
`pg_restore` предупреждает `unrecognized configuration parameter "transaction_timeout"` из-за
**несовпадения версий client (17) и server (16)**.

**Границы:** не менять модели/миграции/stock-логику; не делать web-restore; не менять образ БД на
работающем VPS без бэкапа; не добавлять «ignore all errors».

---

## 1. Как сейчас создаётся `db.dump` (факт из кода)

- `apps/operations/backup.py` → `backup_db()`: для PostgreSQL вызывает `pg_dump -Fc -f <run>/db.dump …`
  (custom-формат), пароль через `PGPASSWORD`.
- `restore_db()`: `pg_restore --clean --if-exists --no-owner -d <db> <db.dump>`.
- Обе утилиты берутся из `PATH` **внутри контейнера `web`** (`shutil.which("pg_dump")`).

## 2. Внутри какого контейнера и какие версии

| Компонент | Где | Версия (проверить командой) |
|---|---|---|
| `pg_dump` / `pg_restore` | контейнер **web** (`docker/Dockerfile`: `apt-get install postgresql-client`) | `docker compose exec web pg_dump --version` — ожидаемо **17.x** (Debian trixie base) |
| PostgreSQL server | контейнер **db** (`docker-compose.yml`: `image: postgres:16`) | `docker compose exec db postgres --version` — **16.x** |

**Первый шаг реализации — зафиксировать фактические версии этими двумя командами** (в отчёте).

## 3. Почему появился `transaction_timeout`

`transaction_timeout` — GUC, добавленный в **PostgreSQL 17**. `pg_dump` 17 включает `SET
transaction_timeout = 0` в архив. При `pg_restore` против сервера **16** сервер этот параметр не
знает → ошибка на одном `SET`, остальное восстанавливается («errors ignored: 1»).

Корень: **клиент новее сервера** (17 vs 16).

## 4. Варианты исправления и выбор

| Вариант | Суть | Оценка |
|---|---|---|
| **A. Пин client = 16 (рекомендуется)** | в `web`-образе ставить `postgresql-client-16` (совпадает с сервером). Дамп 16 не эмитит `transaction_timeout` | ✅ минимальная правка (только `docker/Dockerfile`); без изменения данных/тома; обратно совместимо |
| B. Тот же major отдельным способом | напр. запускать pg_dump/pg_restore из контейнера `db` (там клиент = сервер 16) | ✅ тоже корректно, но меняет место вызова в `backup.py` (сложнее, больше поверхность) |
| C. Перейти на `postgres:17` | обновить сервер до 17 | ⚠️ **отдельный план**: меняет формат `pgdata`, нужен бэкап + миграция тома; не для этого фикса |
| D. Surface warnings restore | `restore_db` явно показывает предупреждения pg_restore, не глотает | ✅ дополняющая мера к A (не замена) |

**Рекомендация:** **A + D**. A — установить `postgresql-client-16` в `docker/Dockerfile` (через
PGDG apt-репозиторий или доступный пакет), чтобы `pg_dump`/`pg_restore` совпадали с сервером 16.
D — сделать restore честным по ошибкам (surface, а не ignore).

## 5. Как протестировать restore без потери данных

- На **тестовом** окружении (не на проде): свежий бэкап `backup_all` → `restore_db --yes` в
  чистую БД → убедиться, что **нет** warnings/errors pg_restore.
- Round-trip тест на SQLite-пути (уже есть в `tests/test_operations.py`) не покрывает Postgres —
  Postgres-совместимость проверяется **вручную/в CI с сервисом postgres:16** (не в обычном pytest,
  чтобы не тянуть Docker в юнит-тесты).
- **Не** проверять на проде разрушительно; сначала бэкап, потом restore в отдельную БД.

## 6. Compatibility-тест (без Docker в юнит-тестах)

- Юнит-уровень: тест, что `docker/Dockerfile` пинует `postgresql-client-16` (grep по Dockerfile) —
  лёгкий, без Docker.
- Интеграционный (опц., CI): job с `postgres:16` + `postgresql-client-16` → backup→restore без
  warnings. Не в обычном `pytest` (хрупко/тяжело).

## 7. Критерии приёмки будущей реализации

1. `restore_db` проходит **без** warnings/errors pg_restore (нет `transaction_timeout`).
2. `backup_all`/`restore_db` работают на чистом окружении (fresh setup).
3. **Существующие бэкапы не ломаются** (старые дампы 17 всё ещё восстановимы — хотя бы с явным
   предупреждением; новые дампы 16 — чисто).
4. **Media restore не затронут** (`restore_media` не меняется).
5. **Нет** изменений stock/inventory/business-логики, моделей, миграций.
6. `pytest`/`ruff`/`makemigrations --check`/`manage.py check` зелёные.

## 8. Что будет закоммичено (по этапам)

- **Сейчас:** только этот план (и сопутствующие docs инцидента/runbook — отдельным doc-слоем).
- **Реализация (после «go»):** правка `docker/Dockerfile` (пин `postgresql-client-16`), опц.
  surface warnings в `restore_db`, лёгкий тест на пин версии, обновление incident-заметки на
  «resolved». Без изменения образа БД и данных на проде без бэкапа.
