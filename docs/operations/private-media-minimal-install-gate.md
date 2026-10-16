# Минимальный выпуск полноты backup: private_media

**Точный application SHA этого выпуска:**
`e35948804069b3fe8ffa3f499fefe0a775541b47`. Инструкция no-retention
подготовлена отдельной операторской веткой и НЕ меняет этот application SHA.
Её запуск требует нового явного подтверждения владельца. После обнаружения
того, что штатная обёртка удаляет старые поколения, запускать
`/usr/local/sbin/denstock-backup-capped` для первого PRE/POST **запрещено**.

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

### Однократный PRE/POST без ротации

Штатная обёртка делает две независимые очистки: перед upload запускает
`backup_all --keep-last 14` (после успешной локальной проверки удаляет старые
завершённые каталоги), затем перед offsite upload запускает возрастную очистку
и при нехватке места удаляет старые удалённые поколения. Сейчас 22 завершённых
и 9 незавершённых локальных поколений: следующий обычный запуск удалил бы
9 завершённых. Не менять обёртку или `.env.backup`, не запускать её для этого
релиза и не применять `rclone sync/purge/delete/cleanup`.

Использовать только проверенный release-only скрипт
`scripts/operations/private_media_release_backup_no_retention.sh` из
операторской ветки. Он передаётся в `bash -s` по SSH и не устанавливается в
production checkout. Оператор сверяет SHA файла и коммит этой инструкции до
запуска. Пример из каталога операторской ветки (заменить host только после
сверки production):

```sh
sha256sum scripts/operations/private_media_release_backup_no_retention.sh
ssh root@185.250.44.206 'bash -s -- pre' \
  < scripts/operations/private_media_release_backup_no_retention.sh
# После обновления только трёх разрешённых сервисов:
ssh root@185.250.44.206 'bash -s -- post' \
  < scripts/operations/private_media_release_backup_no_retention.sh
```

Скрипт берет тот же `/run/lock/denstock-backup.lock`, что и таймерная обёртка,
проверяет точный SHA checkout/runtime для PRE и POST, запускает существующий
`backup_all --trigger manual` **без `--keep-last`**, проверяет подпись
`production-1`, manifest, SHA-256, базы и архивов. POST дополнительно
проверяет инвентарь `private_media.tar.gz`. Upload идет только в новый
`<BACKUP_OFFSITE_TARGET>/<RUN_ID>` через `rclone copy --immutable`; затем
`rclone check --download` фактически читает каждый объект обратно. В скрипте
нет операций удаления/перемещения; при сбое частичное новое поколение
сохраняется для анализа, старые копии не затрагиваются. Повторять сбойный
upload с новым RUN_ID, не перезаписывать частичный префикс.

Перед каждым запуском повторить read-only измерения: `df -B1` для локального
места, размер последнего бэкапа и живого private volume, `rclone size
--s3-versions` для всей удалённой истории, `rclone backend
list-multipart-uploads` для незавершённых загрузок и фактический лимит бакета
в Yandex. Скрипт требует локальный запас `3 × (последний run + private tree +
128 MiB)` и удалённый запас до порога 800 MiB с резервом на один повторный
upload и 128 MiB версий/служебных данных. На 16 октября измерено: локально
8 883 994 624 свободных байта; удалённо 325 573 985 байт **со всеми
версиями**, 0 незавершённых multipart uploads; прежний полный db/media run
46 520 413 байт, private tree 51 253 509 байт. При неизменных размерах
два новых поколения около 46,5 и 97,8 MB оставляют значительный запас,
но это оценка, а не разрешение пропускать повторный замер. `rclone about`
не сообщает квоту Yandex: если текущий hard quota 921 600 000 байт не
подтверждён независимо, STOP. При любом неизвестном multipart/версии или
нехватке места STOP без очистки старых копий.

Таймер - 03:00 Europe/Moscow. Работать в ограниченном окне 04:00-22:00 MSK,
проверить, что `denstock-backup.service` не активен, и не начинать, если
оценка длительности достигает следующего запуска. Общий `flock -n` делает
совпадение процессов fail-closed; если таймерный процесс уже держит lock,
ручной run выходит с кодом 75. Не отключать таймер, не убивать активный
backup. После приёмки проверить, что `denstock-backup.timer` по-прежнему
active и следующий запуск запланирован. Если безопасное окно невозможно,
STOP и отдельно согласовать временную паузу таймера и его восстановление.
Если к 22:00 MSK POST и независимая проверка не завершены, не продолжать
установку/приёмку через ночной запуск без отдельного решения владельца.
Эта процедура защищает старые поколения **только в пределах окна выпуска**;
следующий обычный таймерный run снова применит свою штатную ротацию. Для
долгосрочного сохранения всех старых копий требуется отдельное изменение
retention policy, не входящее в этот выпуск.

Старый PRE всё ещё не содержит private_media. Его пригодность состоит из
подписанного DB/media run плюс независимо расшифрованного Mac capture всех
16 файлов, сверенного по путям, размерам и SHA-256 с актуальным деревом и
ссылками в восстановленной БД; две исторические ссылки остаются в публичном
media. Перед установкой повторно проверить, что live private tree не изменилось
после сверки. При изменении любого файла/ссылки нельзя «склеивать» разные
моменты времени: STOP и согласовать согласованный новый capture. Пять файлов
без прямых ссылок сохранять.

После каждого запуска записать `RUN_ID`, проверить, что все прежние локальные
каталоги и старые Yandex объекты остались неизменными. Потоковый
`rclone check --download` доказывает read-back, но изолированное восстановление
POST выполняется отдельно из **скачанного** remote run на Mac/в другом
isolated PG16 окружении; до его успеха релиз не завершён. Временные
расшифрованные каталоги и Docker-тома удалить по точным именам, затем
проверить их отсутствие. Не объявлять полноценную независимость исходного
кода: старый source.bundle не содержал production SHA.

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

Создать подписанный PRE однократной no-retention процедурой выше, затем проверить
`manage.py verify_backup RUN_ID`, Ed25519 `production-1`, SHA-256 каждого
компонента и отрицательную проверку изменённого manifest на *копии* файла.
Однократная процедура выполняет `rclone check --download` после upload;
это читает фактические удалённые байты. Нельзя считать один лишь
`offsite_status.json=ok` доказательством read-back. Для последующего
изолированного восстановления скачать POST **с Yandex**, а не взять локальный
production run. На изолированной recovery-машине с read-only rclone-доступом,
после проверки `RUN_ID` и target, например:

```sh
umask 077
RUN_ID='2026-10-16_12-00-00'  # заменить на фактический POST
REMOTE='readonly-yandex:denstock-backups'  # точный утверждённый namespace
READBACK_DIR=$(mktemp -d /tmp/denstock-readback.XXXXXX)
rclone copy --immutable "$REMOTE/$RUN_ID" "$READBACK_DIR/$RUN_ID"
# Далее проверить подпись, SHA-256 и восстановить DB + оба media архива
# только в новой изолированной PostgreSQL 16 и новых каталогах.
```

Если нет независимых read-only credentials на recovery-машине, STOP и
согласовать защищённую временную передачу **скачанного из Yandex** поколения;
не подменять его локальной production-копией. Не применять `sync`, `purge` или
удаление удалённых объектов. После проверки удалить только точный
`READBACK_DIR` и изолированные Docker-ресурсы, затем подтвердить отсутствие
расшифрованных копий. Не удалять recovery kit и прежние backup generations.

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

Создать POST через **ту же однократную no-retention процедуру**, что и PRE.
STOP, если она завершилась
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
