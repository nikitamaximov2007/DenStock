# Production deploy runbook (DenStock)

Пошаговый деплой DenStock на чистый VPS + восстановление данных и бэкапы. **Без секретов.**
Секреты (пароли, ключи, `.env`, rclone-конфиг) создаются на сервере и **никогда не коммитятся**.

> Тестовый сервер (для справки): VDSina, Ubuntu 24.04, `/opt/denstock`, TZ `Europe/Moscow`,
> Docker Compose (db=postgres:16, web, proxy=caddy). Значения — пример; на своём сервере свои.

---

## A. Подготовка VPS

1. Тариф: минимальный с ~2 GB RAM (Docker + Postgres). ОС **Ubuntu 24.04**.
2. Swap 2 GB (полезно на малой RAM):
   ```bash
   fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
   echo '/swapfile none swap sw 0 0' >> /etc/fstab
   ```
3. Обновление и базовые пакеты:
   ```bash
   apt update && apt upgrade -y
   apt install -y git curl nano ufw
   timedatectl set-timezone Europe/Moscow
   ```
4. Docker (официальный скрипт) + compose-plugin:
   ```bash
   curl -fsSL https://get.docker.com | sh
   docker ps        # проверка, что демон работает
   docker compose version
   ```

## B. Клонирование

```bash
mkdir -p /opt && cd /opt
git clone <repo-url> denstock
cd /opt/denstock
git status          # чисто
git log --oneline -1  # зафиксировать актуальный commit
```

## C. Настройка `.env` (секреты — только на сервере)

```bash
cp .env.example .env
nano .env
```
Заполнить:
- `DJANGO_SECRET_KEY` — сгенерировать: `python3 -c "import secrets;print(secrets.token_urlsafe(50))"`
- `POSTGRES_PASSWORD` — сгенерировать: `openssl rand -base64 24`
- `DATABASE_URL=postgres://denstock:<POSTGRES_PASSWORD>@db:5432/denstock`
- `DJANGO_SUPERUSER_PASSWORD` — `openssl rand -base64 18` (логин `admin`).
- `DJANGO_ALLOWED_HOSTS` — IP и/или домен (напр. `91.142.73.205` или `example.com`).

**Никогда** не отправляйте `.env` в чат/Git — там пароли и ключ. `.env` уже в `.gitignore`.

**По IP (без домена):**
```
CADDY_SITE_ADDRESS=:80
DJANGO_SECURE_COOKIES=false
DJANGO_CSRF_TRUSTED_ORIGINS=http://<IP>
```
**После домена (с HTTPS от Caddy):**
```
CADDY_SITE_ADDRESS=example.com
DJANGO_SECURE_COOKIES=true
DJANGO_CSRF_TRUSTED_ORIGINS=https://example.com
DJANGO_ALLOWED_HOSTS=example.com
```

## D. Запуск

```bash
docker compose up -d --build
docker compose ps                 # db healthy, web healthy, proxy up
docker compose exec web python manage.py check
docker compose exec web python manage.py ops_check
```
Открыть сайт по `http://<IP>` (или домену) → форма входа; войти админом.

## E. Восстановление данных (из offsite)

> Restore — **CLI под `--yes`** (web-restore пока нет). Порядок важен: сначала БД, потом media.

```bash
# скачать последний бэкап из Yandex Object Storage
rclone lsf yandex-s3:denstock-backups-nikita          # найти последний <run_id>/
rclone copy yandex-s3:denstock-backups-nikita/<run_id> backups/<run_id>

docker compose exec web python manage.py restore_db   backups/<run_id>/db.dump      --yes
docker compose exec web python manage.py restore_media backups/<run_id>/media.tar.gz --yes
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py check
docker compose exec web python manage.py ops_check
docker compose restart web
```
Проверить: виды деталей, фото (`/media/`), раздел «Бэкапы».

> ⚠️ Если при `restore_db` появляется предупреждение `unrecognized configuration parameter
> "transaction_timeout"` — данные восстановятся, но это **несовпадение версий pg_dump/pg_restore
> (17) и сервера (16)**. См. [инцидент](incidents/2026-07-02-pg-restore-transaction-timeout.md) и
> [план 37](../plans/37-postgres-backup-restore-version-compatibility.md).

## F. Бэкапы

- **Ручной экспорт** из UI: `/operations/backups/` → «Экспорт бэкапа» (manifest `type=manual`).
- **Автоматический**: `scripts/operations/backup_offsite.sh` (`type=automatic`) — см.
  [scheduled-offsite-backups.md](scheduled-offsite-backups.md).
- Конфиг: `cp .env.backup.example .env.backup` (на сервере), `BACKUP_OFFSITE_ENABLED=true`,
  `BACKUP_OFFSITE_TARGET=yandex-s3:denstock-backups-nikita`.
- rclone remote (ключи только на сервере): `rclone config` → remote `yandex-s3` (Yandex Object
  Storage), bucket `denstock-backups-nikita`.
- Статус offsite: `backups/offsite_status.json` (`status=ok`), виден в UI.
- Проверка: `bash scripts/operations/backup_offsite.sh` → `cat backups/offsite_status.json` →
  `rclone lsf yandex-s3:denstock-backups-nikita`.

## G. Cron

```bash
crontab -e
```
Строка (ежедневно 03:00 MSK):
```cron
0 3 * * * cd /opt/denstock && /bin/bash scripts/operations/backup_offsite.sh >> /var/log/denstock-backup.log 2>&1
```
```bash
crontab -l                        # проверить
```
Наутро проверить ночной бэкап:
```bash
tail -n 30 /var/log/denstock-backup.log
cat backups/offsite_status.json
rclone lsf yandex-s3:denstock-backups-nikita | tail
```

## H. Firewall (UFW)

```bash
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw enable
ufw status
```
- Проверить **новое** SSH-окно **до** закрытия текущего (чтобы не потерять доступ).
- **Не** открывать наружу PostgreSQL (5432) и порт `web` (8000): БД доступна только внутри
  docker-сети, web — только через proxy (80/443). Открытый Postgres = прямая утечка данных.

## I. Что делать при аварии (VPS умер)

1. Поднять новый VPS (A), установить Docker/Git.
2. `git clone` в `/opt/denstock` (B).
3. Создать `.env` заново (C) — секреты из безопасного места, **не** из бэкапа.
4. `docker compose up -d --build` (D).
5. Настроить rclone remote `yandex-s3` (F).
6. Скачать последний бэкап и восстановить (E): `restore_db --yes` → `restore_media --yes` →
   `ops_check`.
7. Проверить вход, фото, справочники/остатки/движения; настроить cron (G) и firewall (H).

См. также [post-deploy-checklist.md](post-deploy-checklist.md).
