# Автоматические и offsite бэкапы (v1.1.8B)

Host-level скрипт для регулярных бэкапов и отправки их во внешнее хранилище. Реализует Этап B
плана [36](../plans/36-backup-export-import-ux.md). Планировщик и offsite живут **на сервере**,
независимо от веб-приложения (кнопку в вебе в аварии нажать негде).

**Скрипт:** `scripts/operations/backup_offsite.sh`

## Зачем это нужно

- **Автоматический бэкап** создаётся сам, регулярно, **до** аварии.
- **Локальный бэкап не спасает при смерти VPS/диска** — копии лежат там же и погибают вместе с
  сервером. Настоящий disaster recovery = автоматический бэкап **+ offsite-копия** (вне сервера).
- Скрипт помечает бэкап как `type=automatic` и (если включено) отправляет его в offsite,
  записывая статус в `backups/offsite_status.json`, который показывает UI.

## Что делает скрипт

1. `docker compose exec -T web python manage.py backup_all --trigger automatic --keep-last N`.
2. Находит последний `backups/<timestamp>/`, проверяет `manifest.json`.
3. Если offsite **выключен** → пишет статус `not_configured` (exit 0).
4. Если offsite **включён** → `rclone copy` каталога бэкапа в remote; статус `ok`/`failed`.
5. Пишет `backups/offsite_status.json` (без секретов).

Restore он **не** делает, БД напрямую **не** трогает, `.env` **не** читает и **не** отправляет.

## Конфигурация (без секретов)

Скопируйте пример и заполните **на сервере** (`.env.backup` не коммитится):

```bash
cp .env.backup.example .env.backup
```

| Переменная | Смысл |
|---|---|
| `BACKUP_KEEP_LAST` | сколько последних бэкапов держать локально (напр. 14) |
| `BACKUP_OFFSITE_ENABLED` | `true`/`false` — включить отправку наружу |
| `BACKUP_OFFSITE_METHOD` | `rclone` (единственный поддержанный сейчас) |
| `BACKUP_OFFSITE_TARGET` | `remote:bucket/path` — имя rclone-remote + путь (это **не** ключ) |
| `BACKUP_STATUS_FILE` | путь status-файла (по умолчанию `backups/offsite_status.json`) |
| `BACKUP_WEB_SERVICE` | имя сервиса web в docker compose |

**Секреты offsite (ключи/токены) — только в конфиге rclone на сервере, НЕ в Git.**

## Запуск вручную

```bash
# только показать, что будет сделано (без бэкапа и отправки):
bash scripts/operations/backup_offsite.sh --dry-run

# реально создать бэкап (и отправить offsite, если включён):
bash scripts/operations/backup_offsite.sh
```

## Настроить rclone remote (на сервере, ключи не в Git)

```bash
rclone config          # интерактивно создать remote (S3/R2/Yandex/Google Drive/…)
rclone listremotes     # проверить имя, напр. remote:
rclone lsd remote:     # проверить доступ
```

Конфиг сохраняется в `~/.config/rclone/rclone.conf` **на сервере**. В `.env.backup` указывается
только **имя** remote (`BACKUP_OFFSITE_TARGET=remote:denstock-backups`), а не ключи.

## Расписание

**cron (ежедневно 03:00):**
```cron
0 3 * * * cd /path/to/DenisStock && /bin/bash scripts/operations/backup_offsite.sh >> /var/log/denstock-backup.log 2>&1
```

**systemd service + timer:**
```ini
# /etc/systemd/system/denstock-backup.service
[Service]
Type=oneshot
WorkingDirectory=/path/to/DenisStock
ExecStart=/bin/bash scripts/operations/backup_offsite.sh
```
```ini
# /etc/systemd/system/denstock-backup.timer
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
[Install]
WantedBy=timers.target
```
```bash
systemctl enable --now denstock-backup.timer
systemctl list-timers | grep denstock
```

## Как проверить статус

- **Логи:** cron → `/var/log/denstock-backup.log`; systemd → `journalctl -u denstock-backup`.
- **Status-файл:** `cat backups/offsite_status.json` (`status`: `ok`/`failed`/`not_configured`).
- **В UI:** `/operations/backups/` → блок «Offsite-синхронизация» (отправлено / ошибка / не
  настроено). Секреты в UI не показываются — только имя remote.

## Восстановление на новом VPS (из offsite, только CLI)

**Web-restore/import пока не реализован.** Restore — только CLI под `--yes`:

```bash
# новый VPS → Docker + Git → git clone → создать .env (секреты заново) → docker compose up -d --build
rclone copy remote:denstock-backups/<run_id> backups/<run_id>   # скачать offsite-бэкап
docker compose exec web python manage.py restore_db   backups/<run_id>/db.dump      --yes
docker compose exec web python manage.py restore_media backups/<run_id>/media.tar.gz --yes
docker compose exec web python manage.py ops_check
```

- **`.env` хранить отдельно** (не в бэкапе, не в Git); секреты восстанавливаются вручную.
- **Offsite-credentials не в Git** — только в rclone-конфиге на сервере.
- Безопасный веб-мастер restore/import — отдельный будущий этап C (см. план 36).
