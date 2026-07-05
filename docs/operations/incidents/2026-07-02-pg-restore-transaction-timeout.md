# Инцидент: pg_restore `transaction_timeout` (2026-07-02)

**Статус: УСТРАНЕНО (Layer 30 hotfix + hotfix 2, 2026-07-05).** Реализован
[plan 37](../../plans/37-postgres-backup-restore-version-compatibility.md):
в web-образ пинуется `postgresql-client-16` (новые дампы без
`transaction_timeout`), `restore_db` толерантен строго к этой одной известной
ошибке для старых дампов (любая другая ошибка pg_restore фатальна),
`verify_backup` предупреждает заранее. Hotfix 2: старые custom-архивы
pg_dump 17 (формат 1.16) pg_restore 16 не читает вовсе («unsupported version
(1.16) in file header»), поэтому в образе стоит и `postgresql-client-17`, а
restore автоматически делает fallback на него строго по этой ошибке.
См. [restore-runbook](../restore-runbook.md), раздел 3.

Исходная запись (для истории). Severity: low
(данные не потеряны), но опасно оставлять «errors ignored» в disaster-recovery-пути.

## Что произошло

При восстановлении БД из бэкапа на тестовом VPS (`restore_db --yes`) `pg_restore` выдал:

```
pg_restore: error: could not execute query: ERROR: unrecognized configuration parameter "transaction_timeout"
Command was: SET transaction_timeout = 0;
pg_restore: warning: errors ignored on restore: 1
```

После этого **данные появились** — restore фактически сработал (ошибка на одном `SET`
проигнорирована). Но это **technical debt**: в критическом пути восстановления не должно быть
«ignored errors».

## Почему это всё равно проблема

- Disaster recovery должен проходить **без** warnings/errors — иначе неясно, что ещё «тихо»
  проигнорировано.
- `SET transaction_timeout` — не критичный параметр (таймаут транзакции), поэтому данные целы. Но
  сам факт несовместимости версий может в другой раз затронуть что-то важное.

## Вероятная причина: несовпадение версий PostgreSQL client/server

- **Сервер БД:** `postgres:16` (из `docker-compose.yml`, сервис `db`).
- **Клиент (`pg_dump`/`pg_restore`):** ставится в образ `web` (`docker/Dockerfile`:
  `apt-get install postgresql-client` на базе `python:3.12-slim`). База сейчас — свежий Debian
  (trixie), где `postgresql-client` = **17**.
- `pg_dump` **17** (в контейнере `web`) создаёт дамп с директивой `SET transaction_timeout = 0`
  — это **GUC, появившийся в PostgreSQL 17**. Сервер **16** такого параметра не знает → при
  `restore` ошибка на этом `SET`, остальное восстанавливается.

Итог: **клиент новее сервера** (17 vs 16). Бэкап/restore идут клиентом 17 против сервера 16.

## Что нужно проверить (в реализации, plan 37)

- Фактическая версия `pg_dump`/`pg_restore` в контейнере `web`:
  `docker compose exec web pg_dump --version` (ожидаемо 17.x).
- Версия сервера: `docker compose exec db postgres --version` (16.x).
- Где вызывается pg_dump/pg_restore: `apps/operations/backup.py` (`backup_db`/`restore_db`) —
  внутри контейнера `web`, из `PATH`.

## Безопасные варианты исправления (детально — в plan 37)

1. **Выровнять версии client/server** — рекомендуется: ставить в `web` **`postgresql-client-16`**
   (совпадает с сервером 16) вместо unversioned (17). Дампы 16 не эмитят `transaction_timeout`.
2. Использовать `pg_dump`/`pg_restore` **той же major-версии**, что и сервер (эквивалент п.1).
3. Переход на `postgres:17` — **только отдельным планом** и **с бэкапом перед изменением** (меняет
   формат данных на диске тома `pgdata`).
4. **Не** игнорировать ошибки restore молча — сделать так, чтобы `restore_db` явно сообщал о
   warnings pg_restore (surface, а не глотать).

## Что НЕ делать

- **Не** менять образ БД на работающем VPS без предварительного бэкапа.
- **Не** переписывать дамп вручную (удалять `SET transaction_timeout`) как основное решение.
- **Не** добавлять «ignore all pg_restore errors» — это скрыло бы реальные проблемы restore.
