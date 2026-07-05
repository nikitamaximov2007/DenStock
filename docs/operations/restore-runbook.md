# Restore runbook: восстановление DenisStock из бэкапа

Восстановление ПЕРЕЗАПИСЫВАЕТ текущую базу и media. Это аварийная операция:
используйте её только при реальной аварии или переезде на новый сервер.
Есть два пути: CLI (основной, всегда доступен) и защищённый веб-restore
(Layer 30, только для владельца из allowlist).

## 0. Перед любым восстановлением

1. Создайте бэкап текущего состояния (UI «Экспорт бэкапа» или
   `docker compose exec web python manage.py backup_all`).
2. Проверьте целостность бэкапа, который собираетесь восстановить:

```bash
docker compose exec web python manage.py verify_backup <run_id>
```

Команда read-only: печатает отчёт (manifest, файлы, движок, свежесть) и
падает с ошибкой, если бэкап непригоден.

## 1. Путь A: CLI (основной)

```bash
docker compose exec web python manage.py restore_db   backups/<run_id>/db.dump --yes
docker compose exec web python manage.py restore_media backups/<run_id>/media.tar.gz --yes
docker compose exec web python manage.py migrate
docker compose exec web python manage.py ops_check
```

Подробности и сценарий «новый VPS с нуля»: production-deploy-runbook.md.

## 2. Путь B: защищённый веб-restore (Layer 30)

### Кому доступен

Только владельцу-администратору из allowlist и только при включённом флаге.
Все четыре условия одновременно:

1. `DENSTOCK_ENABLE_WEB_RESTORE=true` в `.env` (по умолчанию false);
2. пользователь аутентифицирован;
3. пользователь superuser или роль «Администратор»;
4. email пользователя входит в `DENSTOCK_RESTORE_ALLOWED_EMAILS` (по
   умолчанию nikita.maximov2007@gmail.com) ИЛИ username входит в
   `DENSTOCK_RESTORE_ALLOWED_USERNAMES`.

Обычные администраторы и все остальные роли restore-блок НЕ видят, а прямые
запросы на restore-URL получают 403. Загрузки внешних файлов бэкапов нет:
восстановление только из существующих локальных бэкапов DenisStock.

### Как включить (на сервере)

```bash
# в .env добавить:
DENSTOCK_ENABLE_WEB_RESTORE=true
DENSTOCK_RESTORE_ALLOWED_EMAILS=nikita.maximov2007@gmail.com
# перезапустить web:
docker compose up -d web
```

Рекомендация: включать флаг только на время восстановления, потом выключить.

### Как выполняется

1. «Бэкапы» -> блок «Аварийное восстановление» -> «Проверить бэкап» у нужной
   строки (видны дата, тип, размер, наличие manifest/БД/media, метка «старый»).
2. Экран проверки: manifest, файлы, движок, контрольные суммы (если есть в
   manifest), совместимость версии, свежесть. Ошибки блокируют восстановление.
3. Подтверждение: ввести слово ПОДТВЕРЖДАЮ (заглавными) и отметить checkbox
   «Я понимаю, что текущие данные будут перезаписаны». POST + CSRF; GET
   никогда ничего не запускает.
4. Система создаёт pre-restore бэкап текущей базы и media (тип pre_restore).
   Если он не создался, восстановление НЕ выполняется.
5. Затем: восстановление БД (pg_restore, тот же путь, что CLI), восстановление
   media, `migrate`. Соединения Django закрываются перед restore; обычная
   транзакция Django не используется (restore идёт внешними инструментами).
6. Страница результата: статус, журнал шагов, рекомендации (войти заново,
   проверить Главную/Остатки/Движения/Статистику, создать новый бэкап).

### Журнал

- Таблица «Журнал восстановлений» в разделе «Бэкапы» (кто, какой бэкап,
  какой pre-restore, статус, ошибка, время): модель RestoreJob.
- Файл `backups/restore.log`: дублирующий след, переживает перезапись базы.

### Откат, если восстановили не то

Pre-restore бэкап хранит состояние ДО восстановления. Его run_id виден в
журнале восстановлений и в restore.log. Откат: восстановить этот pre-restore
бэкап (через тот же веб-restore или CLI путь A).

### Если восстановление упало

- Ошибка ДО шага restore (проверка, pre-restore бэкап): текущая база не
  тронута, ничего делать не надо.
- Ошибка ВО ВРЕМЯ restore: база может быть в неполном состоянии. Немедленно
  восстановите pre-restore бэкап по пути A (CLI). Его run_id показан на
  странице ошибки и в `backups/restore.log`.

## 3. Совместимость версий PostgreSQL (transaction_timeout)

Симптом из drill 2026-07-05: restore падал с
`unrecognized configuration parameter "transaction_timeout"`, хотя данные
восстанавливались. Причина: дамп сделан клиентом pg_dump 17, а сервер
PostgreSQL 16 не знает параметр `transaction_timeout` (появился в 17).

Дополнительный симптом (hotfix 2): после пина клиента 16 старый архив,
созданный pg_dump 17, перестал открываться вовсе:
`pg_restore: error: unsupported version (1.16) in file header`. Причина:
custom-архив pg_dump 17 имеет формат 1.16, а pg_restore 16 читает только
форматы до 1.15. То есть старые архивы 17 может прочитать только pg_restore 17.

Исправлено (hotfix Layer 30 + hotfix 2):

- **Новые дампы чистые:** web-образ ставит `postgresql-client-16` из PGDG
  (та же major-версия, что сервер `postgres:16`); `backup_all`/`backup_db`
  вызывают явный `/usr/lib/postgresql/16/bin/pg_dump`. Такие дампы не
  содержат `SET transaction_timeout` и читаются pg_restore 16.
- **Старые архивы pg_dump 17 восстанавливаются через fallback:** в образе
  стоит и `postgresql-client-17`. `restore_db` сначала запускает
  `/usr/lib/postgresql/16/bin/pg_restore`; если stderr содержит ровно
  «unsupported version (1.16) in file header», повторяет восстановление
  клиентом `/usr/lib/postgresql/17/bin/pg_restore` и пишет предупреждение.
- **transaction_timeout остаётся честным предупреждением:** если
  единственная ошибка restore (в т.ч. после fallback) - пропущенный
  `SET transaction_timeout`, restore завершается успешно с предупреждением.
  ЛЮБАЯ другая ошибка pg_restore фатальна: это не «ignore all errors».
- **verify_backup предупреждает заранее:** определяет формат архива по
  заголовку (PGDMP + версия формата; 1.16 = нужен fallback pg_restore 17)
  и наличие `SET transaction_timeout` в дампе. Read-only.
- **ops_check показывает фактические пути и версии** pg_dump/pg_restore
  (включая fallback-клиент 17), а не просто «доступны».

После обновления пересоберите образ: `docker compose build web`.

## 4. Известные ограничения

- Веб-restore выполняется синхронно в запросе: для маленькой базы DenisStock
  это секунды; страницу не закрывать до результата.
- Загрузка внешнего файла бэкапа через браузер не реализована (сознательно):
  внешний бэкап сначала кладётся в `backups/<run_id>/` на сервере (scp/rclone),
  затем восстанавливается.
- Несовпадение версий PostgreSQL client/server: см. план 37 и инцидент
  2026-07-02 (transaction_timeout).
