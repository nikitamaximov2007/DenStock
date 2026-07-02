# Post-deploy checklist (DenStock)

Пройти после деплоя/восстановления на VPS. Подробности — в
[production-deploy-runbook.md](production-deploy-runbook.md).

## Приложение
- [ ] `docker compose ps` — db **healthy**, web **healthy**, proxy **up**
- [ ] Сайт открывается (`http://<IP>` или домен)
- [ ] Вход администратором работает
- [ ] `manage.py check` — без ошибок
- [ ] `ops_check` — все проверки OK

## Данные и media
- [ ] Данные восстановлены (виды деталей, справочники, остатки, движения видны)
- [ ] Media восстановлены: `/app/mediafiles` содержит файлы (count > 0), фото открываются
- [ ] Фото отдаются по `/media/` через Caddy

## Бэкапы (локально)
- [ ] Последний бэкап содержит `db.dump` (или `db.sqlite3` в dev)
- [ ] Последний бэкап содержит `manifest.json`
- [ ] Последний бэкап содержит `media.tar.gz` (если media есть)
- [ ] Ручной «Экспорт бэкапа» из UI работает

## Offsite
- [ ] `backups/offsite_status.json` → `status: ok`
- [ ] `rclone lsf yandex-s3:denstock-backups-nikita` показывает последний `<run_id>/`
- [ ] В UI (`/operations/backups/`) offsite-статус = «отправлено»

## Расписание и firewall
- [ ] Cron установлен (`crontab -l` содержит строку `backup_offsite.sh` на 03:00)
- [ ] Логи пишутся (`/var/log/denstock-backup.log`)
- [ ] UFW включён (`ufw status` = active; OpenSSH/80/443 allow; 5432/8000 наружу закрыты)
- [ ] Новое SSH-подключение работает (доступ не потерян)

## Безопасность репозитория
- [ ] `git status` чистый
- [ ] В Git **нет** секретов: `.env`, `.env.backup`, `rclone.conf`, `backups/` не отслеживаются
- [ ] Пароли/ключи в чат/Git не отправлялись

## Известные замечания
- [ ] Учтён инцидент `transaction_timeout` при `restore_db`
      ([incident](incidents/2026-07-02-pg-restore-transaction-timeout.md),
      [plan 37](../plans/37-postgres-backup-restore-version-compatibility.md))
