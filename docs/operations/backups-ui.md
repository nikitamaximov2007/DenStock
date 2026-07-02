# Backup UI — раздел «Бэкапы» (v1.1.7)

Веб-раздел `/operations/backups/` для owner/admin. Первый **безопасный** слой: только
локальные бэкапы (создать / посмотреть / скачать / проверить manifest). Проектное обоснование
и disaster-recovery — в [docs/plans/35-backups-ui-and-offsite.md](../plans/35-backups-ui-and-offsite.md).

## Что делает UI

- **Список** локальных backup-run из `BACKUP_ROOT` (`backups/<timestamp>/`): наличие `manifest.json`,
  `db.dump`/`db.sqlite3`, `media.tar.gz`, размеры файлов, поля manifest (created_at/engine/version/
  git_commit).
- **«Экспорт бэкапа»** (синяя primary-кнопка, POST, CSRF) — создаёт **ручной** локальный бэкап
  текущего состояния через существующий `apps/operations/backup.backup_all(trigger="manual")`.
  Операция может занять время.
- **Manifest** и **Скачать** — просмотр манифеста и выгрузка файлов **только** из конкретного
  backup-run (разрешены `manifest.json`, `db.dump`, `db.sqlite3`, `media.tar.gz`; защита от path
  traversal).
- **Статус offsite** — read-only: если есть `backups/offsite_status.json`, показывается; иначе
  «не настроено».

## Типы бэкапов (manifest `type`)

В `manifest.json` есть поле **`type`** (v1.1.8A). В списке и в manifest-view показывается бейджем:

| `type` | Бейдж | Смысл |
|---|---|---|
| `manual` | Ручной | создан человеком через «Экспорт бэкапа» или `backup_all` |
| `automatic` | Автоматический | создан планировщиком: `backup_all --trigger automatic` (для будущего scheduled-скрипта, этап B) |
| `pre_restore` | Перед восстановлением | аварийный снимок перед restore (будущий restore-wizard, этап C) |
| `uploaded` | Загруженный | залит файлом (будущий импорт, этап C) |
| нет поля | Legacy | старый бэкап без поля `type` (не ошибка) |
| иное | Неизвестный тип | значение вне списка |

CLI уже поддерживает `python manage.py backup_all --trigger automatic` — это задел для
scheduled-скрипта (этап B). Реально в этом слое UI создаёт только `manual`.

## Почему локального бэкапа недостаточно

Локальные бэкапы лежат на том же сервере/диске. При смерти VPS/диска они теряются вместе с ним, а
кнопку в вебе будет негде нажать. Настоящий disaster recovery = **scheduled backup + offsite copy**
(вне сервера). Планировщик и offsite-синхронизация делаются **на уровне хоста** (cron/systemd +
rsync/rclone), независимо от веб-приложения. Offsite-провайдер — **отдельный будущий слой**
(в этом слое не реализуется).

## Web-restore специально отсутствует

Через UI **нельзя** восстановить БД/media: это может затереть текущие данные, оборвать сессии или
упасть на середине. **Restore — только CLI** под `--yes`:

```bash
docker compose exec web python manage.py restore_db   backups/<run_id>/db.dump      --yes
docker compose exec web python manage.py restore_media backups/<run_id>/media.tar.gz --yes
docker compose exec web python manage.py ops_check
```

## Восстановление на новом сервере (кратко)

1. Новый VPS → Docker + Git.
2. `git clone`, создать `.env` (секреты заново, не из бэкапа).
3. `docker compose up -d --build`.
4. Положить бэкап в `backups/<run_id>/`.
5. `restore_db --yes` → `restore_media --yes` → `ops_check`.
6. Проверить вход, фото (`/media/`), справочники/остатки/движения.

## Доступ и границы

- Раздел виден и доступен **только** owner/admin (`user.is_admin` = superuser или роль
  «Администратор»). Продавцу/Кладовщику/Наблюдателю/Руководителю — нет.
- Backup-файлы (`backups/`) **не коммитятся** в Git (`.gitignore`).
- Без Celery/Redis, без S3/rclone, без шифрования — это отдельные будущие слои.
