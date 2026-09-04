# Ночное резервное копирование production

Здесь лежит то, что на production выполняет ежедневный подписанный бэкап и
отправку копии в Yandex Object Storage. Файлы сняты с работающего сервера и
хранятся побайтово такими, какими он их исполняет.

| Файл в Git | Куда ставится | Владелец | Режим |
|---|---|---|---|
| `bin/denstock-backup-capped` | `/usr/local/sbin/denstock-backup-capped` | `root:root` | `755` |
| `systemd/denstock-backup.service` | `/etc/systemd/system/denstock-backup.service` | `root:root` | `644` |
| `systemd/denstock-backup.timer` | `/etc/systemd/system/denstock-backup.timer` | `root:root` | `644` |

Скрипт лежит не в каталоге проекта, а в `/usr/local/sbin`, потому что unit
запускается от root и не должен зависеть от того, какая версия кода сейчас
выкачана в `/opt/denstock`. Обновление приложения не подменяет механизм бэкапа.

## Что делает скрипт

1. Берёт `flock` на `/run/lock/denstock-backup.lock`. Два бэкапа одновременно
   не идут.
2. Читает `/opt/denstock/.env.backup`, если он есть; иначе работает на
   значениях по умолчанию и остаётся только локальным.
3. Создаёт бэкап штатной командой внутри контейнера `web`:
   `manage.py backup_all --trigger automatic --keep-last "$BACKUP_KEEP_LAST"`.
   Подпись манифеста делает само приложение, ключом из внешнего каталога.
4. Проверяет, что каталог рана появился и в нём есть `manifest.json`.
5. При включённом offsite считает размер бакета через
   `rclone size --s3-versions`, отправляет ран через `rclone copy` и пишет
   `backups/offsite_status.json` для интерфейса.
6. Держит удалённые копии по правилу: всё моложе суток, по одной самой свежей
   за каждый из предыдущих шести дней, дальше по одной за неделю.
7. Лишние поколения убирает `rclone purge` и следом
   `rclone backend cleanup-hidden`. На версионированном бакете `purge` только
   ставит delete-marker, и без `cleanup-hidden` место продолжало бы
   расходоваться и оплачиваться.
8. Любая ошибка пишет статус `failed` и завершает работу ненулевым кодом:
   молча деградировать до «бэкапа нет» нельзя.

Мягкий предел объёма бакета отдельно от жёсткого лимита самого хранилища:
скрипт останавливается раньше, чем провайдер начнёт отказывать в записи.

## Установка на новый сервер

Выполняется один раз, после того как `/opt/denstock` развёрнут и контейнеры
подняты. Команды намеренно не собраны в отдельный установщик: их четыре, и
запуск бэкапа не должен происходить как побочный эффект установки.

```bash
install -o root -g root -m 755 deploy/backup/bin/denstock-backup-capped \
  /usr/local/sbin/denstock-backup-capped
install -o root -g root -m 644 deploy/backup/systemd/denstock-backup.service \
  /etc/systemd/system/denstock-backup.service
install -o root -g root -m 644 deploy/backup/systemd/denstock-backup.timer \
  /etc/systemd/system/denstock-backup.timer
systemctl daemon-reload
```

Проверить разбор unit-файлов, ничего не запуская:

```bash
systemd-analyze verify /etc/systemd/system/denstock-backup.service
systemd-analyze verify /etc/systemd/system/denstock-backup.timer
```

Включить расписание можно только после того, как `/opt/denstock/.env.backup`
создан из `.env.backup.example` и заполнен, а remote в конфиге rclone заведён.
Без `.env.backup` служба не запустится: в unit стоит `ConditionPathExists`, и
systemd просто пропустит запуск, а не упадёт.

```bash
systemctl enable --now denstock-backup.timer
systemctl list-timers denstock-backup.timer
```

`enable --now` включает **таймер**, а не бэкап. Первый бэкап произойдёт в
ближайшие 03:00 по Москве. Немедленный прогон делается отдельно и осознанно
через `systemctl start denstock-backup.service`; на новом сервере это стоит
делать вручную, когда есть кому смотреть на результат.

## Что остаётся снаружи Git

- `/opt/denstock/.env.backup` - имена переменных описаны в
  `.env.backup.example` в корне репозитория, значения заполняются на сервере.
- Конфигурация rclone с ключами Object Storage, обычно
  `/root/.config/rclone/rclone.conf`.
- Каталог подписи `/etc/denstock/manifest-signing` с приватным ключом. Само
  монтирование описано в отслеживаемом `docker-compose.signing.yml`.
- Сами резервные копии: и локальные в `backups/`, и то, что лежит в бакете.
- Журнал `/var/log/denstock-backup.log`, который создаёт служба.

## Проверка после установки

```bash
systemctl is-enabled denstock-backup.timer
systemctl is-active denstock-backup.timer
systemctl list-timers denstock-backup.timer
cat /opt/denstock/backups/offsite_status.json
```

Ожидаемое расписание: ежедневно 03:00 `Europe/Moscow`, `Persistent=true`, то
есть пропущенный из-за выключенного сервера запуск догоняется после включения.
