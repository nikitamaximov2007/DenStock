# Минимальный выпуск полноты backup: private_media

> **Операторская процедура для текущего дизайна заменена.** Для PRE/POST
> используйте [один Yandex bucket и зашифрованные копии на Mac](private-media-single-bucket-mac-release.md).
> Не выполняйте нижеописанные команды PRE/POST: они используют capped wrapper,
> который удаляет старые поколения до проверки новой загрузки в Yandex.

Этот runbook относится только к ветке `codex/infra-release-gate-web-restore`.
Проверенный production baseline на 2026-10-15:
`f7b80861fc1d81f6be70c86e78be7f06418a82d2`; исходное исправление
online-restore: `a3f76861c971b10bbc09d809999e75c71e1fe5f8`.
**До отдельного разрешения владельца ничего из разделов PRE, установка и POST не
исполнять.** Точный 40-символьный SHA кандидата фиксируется в отчёте квалификации;
перед установкой оператор подставляет именно его в `APPROVED_SHA` и проверяет
`git rev-parse origin/codex/infra-release-gate-web-restore`. Документ входит в
сам коммит, поэтому записать его собственный SHA внутри него невозможно.

Изменение: подписанный backup включает `db.dump`, `media.tar.gz`,
`private_media.tar.gz`, manifest с SHA-256; production web-restore закрыт даже
при включённом флаге. Общий `cleanup_attachment` изменён, его вызывают
`web`, `telegram-bot` и `max-bot`. Требуется пересоздать **все три** из одного
утверждённого SHA. Не пересоздавать PostgreSQL, `catalog-web`, proxy/Caddy.
Не устанавливать новый `denstock-backup-capped-safe.sh`: действующая обёртка и
расписание остаются прежними. Миграций у кандидата нет.

## Предварительные STOP-условия

Остановиться без установки, если хотя бы одно не выполнено:

1. Есть отдельное разрешение владельца на точный SHA и три сервиса.
2. Production HEAD и `DENSTOCK_APP_COMMIT` совпадают с зарегистрированным
   исходным SHA; `origin/main` не изменился неожиданно; параллельного релиза нет.
3. Рабочее дерево production и production-only файлы (`docker-compose.signing.yml`,
   `.env*`, `vhome.http`, если существует) инвентаризированы и сохранены.
4. Образы всех трёх текущих сервисов идентифицированы и сохранены под
   отдельными rollback-тегами. Зафиксированы их прежние разные commit SHA.
5. `manage.py showmigrations --plan` на production и план кандидата не
   показывают ожидающих, конфликтующих или новых миграций. При расхождении
   STOP до запуска web: штатный entrypoint может вызвать `migrate --noinput`.
6. Достаточно места для PRE, POST, приватного архива и удалённого read-back;
   есть подтверждённая независимая копия всех текущих private attachments.
   Старый PRE **не содержит private_media** и сам по себе не является полной
   точкой восстановления. Снимок private_media должен быть согласован с PRE,
   проверен по файлам/ссылкам БД и сохранён вне VPS; если это нельзя обеспечить
   без безопасного окна записи, остановиться и согласовать окно с владельцем.
7. PRE-подпись `production-1`, локальные SHA-256, декодирование полного
   `db.dump`, целостность `media.tar.gz`, отрицательная проверка подменённого
   manifest, доступность Yandex и фактическое скачивание PRE подтверждены.
8. Никаких изменений ключей, секретов, расписания, Caddy, конфигурации Yandex
   или данных склада не планируется.

## PRE и фиксация состояния

Команды выполнять в `/opt/denstock`, без `set -x` и без печати `.env*`:

```sh
git rev-parse HEAD
git status --short
git ls-remote origin refs/heads/main refs/heads/codex/infra-release-gate-web-restore
docker compose ps
docker compose config --services
docker compose exec -T web python manage.py showmigrations --plan
sha256sum docker-compose.signing.yml
systemctl status denstock-backup.timer --no-pager
```

Отдельно проверить текущий commit каждого контейнера, image ID и сохранённые
rollback-теги, не печатая полный `docker inspect` (в нём могут быть секреты).
Сохранить старые образы до `build`, записав `ROLLBACK_ID` в журнал релиза:

```sh
ROLLBACK_ID=$(date -u +%Y%m%dT%H%M%SZ)
for service in web telegram-bot max-bot; do
    cid=$(docker compose --profile telegram-bot --profile max-bot ps -q "$service")
    test -n "$cid" || exit 1
    image_id=$(docker inspect --format '{{.Image}}' "$cid")
    docker image tag "$image_id" "denstock-${service}:rollback-${ROLLBACK_ID}"
done
```

Создать штатный подписанный PRE действующей обёрткой, затем проверить
`manage.py verify_backup RUN_ID`, Ed25519 `production-1`, SHA-256 каждого
компонента и отрицательную проверку изменённого manifest на *копии* файла.
Обёртка сейчас подтверждает только размеры/число файлов, поэтому скачать
весь PRE из Yandex в отдельный временный каталог и сравнить каждый байт с
локальным оригиналом (`rclone check --download` плюс локальные SHA-256).
Нельзя считать один лишь `offsite_status.json=ok` доказательством read-back.
После проверки `RUN_ID` и источника offsite оператор выполняет, например:

```sh
set -a; . ./.env.backup; set +a
RUN_ID='2026-10-15_03-00-20'  # заменить на фактический новый PRE или POST
READBACK_DIR=$(mktemp -d /tmp/denstock-readback.XXXXXX)
rclone check --download "backups/$RUN_ID" "$BACKUP_OFFSITE_TARGET/$RUN_ID"
rclone copy "$BACKUP_OFFSITE_TARGET/$RUN_ID" "$READBACK_DIR/$RUN_ID"
for component in manifest.json db.dump media.tar.gz; do
    cmp -s "backups/$RUN_ID/$component" "$READBACK_DIR/$RUN_ID/$component" || exit 1
done
# В POST дополнительно: cmp -s для private_media.tar.gz или STOP.
```

Запускать команды только при непустом и проверенном `BACKUP_OFFSITE_TARGET` и
существующем `RUN_ID`; не применять `sync`, `purge` или удаление удалённых
объектов. Временный скачанный каталог сохраняется до завершения проверки.

## Установка после PRE

Перед изменением checkout ещё раз сравнить approved SHA, его parent chain,
`origin/main`, production HEAD и отсутствие другого оператора. Не применять
миграции вручную. Сохранить production-only файлы и подписывающие ключи.
После проверки плана миграций установить только точный утверждённый commit,
обновить `DENSTOCK_APP_COMMIT` тем же SHA, собрать `web`, `telegram-bot`,
`max-bot` и запустить **только** их через Compose с `--no-deps`. У ботов
заданы Compose profiles: запускать с соответствующими профилями. При
неожиданной миграции остановиться и не продолжать. Не менять main до полной
приёмки; не использовать force push, `reset --hard`, `clean -fd`.

Команды пересоздания после всех STOP-проверок:

```sh
docker compose --profile telegram-bot --profile max-bot build web telegram-bot max-bot
docker compose --profile telegram-bot --profile max-bot up -d --no-deps --wait \
    --force-recreate web telegram-bot max-bot
```

После пересоздания подтвердить image ID и `DENSTOCK_APP_COMMIT` каждого из
трёх сервисов, health/heartbeat ботов, `/healthz/` (внутренний и публичный),
отсутствие ошибок startup/webhook/sender. Для `catalog-web`, db, proxy
сравнить container ID до/после: они не должны измениться. Проверить, что
web-restore недоступен в production даже при прежнем включённом флаге, но
страницы просмотра/проверки backup работают. Не запускать реальное
восстановление и не создавать бизнес-документы ради проверки.

## POST и приёмка полного recovery point

Создать POST через **ту же существующую** обёртку. STOP, если она завершилась
ошибкой, удалённый ран неполон или не скачивается. В POST должны быть ровно
компоненты manifest: `db.dump`, `media.tar.gz`, `private_media.tar.gz` и
`manifest.json`. Проверить:

- manifest подписан `production-1`, отрицательный tamper-контроль отклонён;
- все SHA-256, `private_media_status=included`, число файлов и дерево архива
  совпадают с manifest; ссылки из восстановленного снимка БД на приватные
  вложения разрешаются в восстановленном архиве. Не сравнивать POST с живым
  томом после возобновления записей: за это время он мог законно измениться;
- весь `db.dump` читается `pg_restore` 16; оба tar читаются целиком;
- `verify_backup` возвращает PASS без предупреждения об отсутствии приватных
  файлов; удалённый POST скачан целиком и **каждый файл** совпал по SHA-256;
- из скачанного POST выполнена изолированная проверка восстановления в
  PostgreSQL 16 и двух пустых медиа-каталогах, без Telegram/MAX, сетевых
  вызовов к клиентам или подключения к production DB;
- health и боты остались здоровы, `origin/main` продвинут только обычным
  fast-forward после успешной приёмки, три SHA (`HEAD`, `DENSTOCK_APP_COMMIT`,
  `origin/main`) равны утверждённому SHA.

Один только локальный POST не закрывает gate: нужен фактический Yandex
read-back. Не удалять прежние подписанные поколения при сбое нового.

## Сбой и откат

При любой ошибке прекратить продвижение, сохранить безопасные логи, точные
SHA/container ID и состояние PRE/POST. **Без автоматического отката базы.**
Миграций нет, поэтому после оценки причины можно вернуть именно сохранённые
старые образы `web`, `telegram-bot`, `max-bot` и старые runtime SHA, затем
пересоздать только эти три сервиса с `--no-deps`; не пытаться собрать всех
ботов из старого web SHA, поскольку до выпуска их версии различались.
Compose использует теги `denstock-web:latest`,
`denstock-telegram-bot:latest`, `denstock-max-bot:latest`. Перед сборкой
записать image ID каждого действующего контейнера и присвоить ему отдельный
rollback-тег. При согласованном откате вернуть именно эти три image ID под
теги `:latest`, вернуть прежние production HEAD и `DENSTOCK_APP_COMMIT` и
выполнить `docker compose --profile telegram-bot --profile max-bot up -d
--no-deps --no-build --force-recreate web telegram-bot max-bot`. Затем
проверить health и вернуться к разбору причины. Команду отката не запускать
автоматически при первом сбое.
Возврат тегов после отдельной оценки причины:

```sh
for service in web telegram-bot max-bot; do
    docker image tag "denstock-${service}:rollback-${ROLLBACK_ID}" \
        "denstock-${service}:latest" || exit 1
done
```

PostgreSQL, `catalog-web` и proxy не трогать. Если старые образы не сохранены,
подписанный PRE неполон, появились записи/миграции или невозможно однозначно
определить целевой image ID, STOP и отдельный план восстановления с владельцем.
Не выполнять опасный rollback вслепую.
