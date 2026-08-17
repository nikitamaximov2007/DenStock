# Аварийный локальный режим DenisStock

## Назначение и границы

Режим нужен, когда складской компьютер не видит production DenisStock, но склад
физически продолжает работать. Первая версия использует только модель
`single writer / controlled failover`:

- до перехода пишет production;
- во время аварийной сессии пишет только local emergency instance;
- после сессии local замораживается;
- возврат возможен только после доказательства общего предка и отсутствия
  независимых production writes;
- database merge не выполняется;
- production restore никогда не запускается автоматически.

ИИ-поддержка и внешние integrations в автономном режиме выключены. Основные
складские функции работают с локальными PostgreSQL и media.

## Критические запреты

Нельзя:

- одновременно проводить складские операции в production и local;
- запускать local с произвольного или непроверенного dump;
- менять `control.json` вручную;
- подключать emergency-local к production DB host;
- сравнивать базы только по commit SHA или числу строк;
- восстанавливать local package в production при `CONFLICT` или `BLOCKED`;
- очищать final export до подтверждённого `COMPLETED`;
- открывать production writes после restore в обход finalizer;
- коммитить `.env.emergency`, rclone config, probe token или DB password.

## Компоненты

| Компонент | Назначение |
|---|---|
| `docker-compose.emergency.yml` | Изолированные PostgreSQL 16, Django и Caddy в WSL2 Docker Engine |
| `.env.emergency` | Локальные secrets, identity и offsite source |
| `.emergency/control.json` | Активная standby, previous standby и lifecycle marker |
| `.emergency/standbys/` | Проверенные DB slots и media snapshots |
| `.emergency/backups/` | Final offline exports |
| `.emergency/packages/` | Пакеты для review/reconciliation/failback |
| `DenisStock-Emergency.ps1` | Операторское меню |
| `Emergency-Standby-Refresh.ps1` | Неинтерактивный scheduled standby refresh |

Emergency Compose по умолчанию слушает только `127.0.0.1`, порт `8080`. Только
provisioned Primary может намеренно слушать свой fixed LAN IPv4; Windows firewall
при этом ограничен `LocalSubnet`. Он не использует production Compose volumes и
не публикует PostgreSQL port. Docker Desktop не является required runtime:
supported workstation path описан в
[emergency-workstation-deployment.md](emergency-workstation-deployment.md).

## Production prerequisites

До первого emergency-capable backup в production `.env` должны быть заданы:

- `DENSTOCK_APP_COMMIT` - полный SHA развёрнутого release;
- `DENSTOCK_INSTANCE_ID` - стабильное имя production instance;
- `DENSTOCK_PRODUCTION_DB_HOSTS=db,185.250.44.206` или более узкий фактический
  allowlist;
- `DENSTOCK_EMERGENCY_PROBE_TOKEN` - отдельный случайный read-only probe token;
- `DENSTOCK_PRODUCTION_URL=https://185-250-44-206.sslip.io`.

`DENSTOCK_MODE=production` задаётся явно или берётся как default из production
settings. Startup блокируется, если production использует не PostgreSQL,
неразрешённый host или emergency-prefixed database. Probe token не является DB
credential и не даёт права на restore или mutation.

## Первичная настройка Windows

Поддерживаемая workstation-установка использует WSL2 Ubuntu и Docker Engine
внутри WSL. Docker Desktop не требуется. Выполните только
[workstation deployment guide](emergency-workstation-deployment.md) от имени
ответственного администратора: installer создаёт local secrets, `.env.emergency`,
ACL, firewall rule, shortcuts и scheduled refresh. Обычный сотрудник не должен
видеть `.env.emergency`, вводить PostgreSQL credentials или запускать PowerShell.

Для Yandex Object Storage нужен `rclone`, настроенный только в профиле
ответственного Windows-пользователя. Production DB credentials в emergency env
не нужны и запрещены. Проверка окружения аварийно завершает startup, если mode,
DB host или DB name не соответствуют local allowlist и prefix
`denstock_emergency_`.

Manual local setup below is retained only for an isolated development drill,
not for a warehouse workstation. It must not create a second primary writer.

## Manifest schema v2

Каждый пригодный backup содержит `manifest.json` со следующими обязательными
данными:

- `schema_version`, `backup_run_id`, `created_at`, `verified_at`;
- `source_environment`, `source_instance_id`, `storage_origin`;
- `app_commit`;
- безопасное имя DB и имя dump без credentials;
- SHA-256 database dump;
- имя и SHA-256 media archive, если media непусты;
- SHA-256 восстановленного media tree;
- applied migration list и migration fingerprint;
- permanent `database_identity`;
- `business_generation`;
- общий business SHA-256;
- count, max primary key и SHA-256 по каждой business model;
- `consistency` и `verification_status`.

Final local manifest дополнительно содержит `offline_lineage`: session id,
base backup run, base database identity, base business hash и base media hash.
Secrets в manifest отсутствуют.

## Обновление standby

В меню выбрать `Обновить аварийную копию`. Процесс:

1. Находит последний run в local source или rclone remote.
2. Скачивает его в временный каталог.
3. Проверяет manifest schema, source environment, SHA-256 и verified status.
4. Проверяет exact app commit и migration fingerprint.
5. Создаёт отдельную candidate DB с безопасным prefix.
6. Восстанавливает PostgreSQL dump и media.
7. Запускает `manage.py check`, migration check, stock balance check и probe.
8. Сверяет business и media tree fingerprints с manifest.
9. Только после успеха atomically меняет active pointer.
10. Сохраняет previous verified standby.

При создании PostgreSQL backup DenisStock берёт exclusive failover lock на
время вычисления data/media markers, `pg_dump` и media archive. Новые
application writes ждут завершения snapshot. Standby всё равно восстанавливает
dump в отдельную DB и повторно сравнивает fingerprints до activation.

При любой ошибке active standby не меняется, candidate DB удаляется. Cleanup
старой копии не может отменить уже подтверждённую activation.

Status показывает время backup, возраст, production commit, run id, instance и
write state. Порог предупреждения задаёт
`DENSTOCK_EMERGENCY_STALE_WARNING_HOURS`; устаревшая копия не запускается
автоматически, решение остаётся у ответственного лица.

## Scheduled refresh

Для Windows Task Scheduler используется:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File F:\DenStock\scripts\operations\Emergency-Standby-Refresh.ps1
```

Рекомендуемый стартовый режим - один раз утром и один раз вечером при входе в
систему. Интервал согласовать с допустимой потерей данных. Если компьютер
выключен, задача просто не выполняется. Если offline lifecycle начат, scheduled
refresh fail-closed и не меняет local DB.

## Planned failover

Planned transition даёт наиболее сильную гарантию single writer.

1. Остановить новые операции и уведомить сотрудников.
2. На production включить maintenance lock:

   ```bash
   docker compose exec web python manage.py production_maintenance \
     --enable --confirm 'PRODUCTION-ТОЛЬКО-ЧТЕНИЕ' \
     --reason 'planned local failover'
   ```

3. Проверить, что write-запрос получает HTTP 423, а `/healthz/` работает.
4. Создать fresh production backup под lock и отправить его offsite.
5. На Windows выполнить standby refresh и проверить status.
6. В меню выбрать `Запустить автономный режим`, тип `planned`.
7. Ввести точную confirmation phrase.
8. Открыть local DenisStock и убедиться, что banner показывает
   `АВТОНОМНЫЙ РЕЖИМ`.
9. Оставить production в maintenance до решения failback.

Если local start не состоялся, production можно открыть только после проверки,
что local session не была создана:

```bash
docker compose exec web python manage.py production_maintenance \
  --disable --confirm 'ОПАСНО-РАЗРЕШИТЬ-ЗАПИСЬ'
```

## Unplanned failover

Если связь уже потеряна, перевести production в maintenance невозможно.

1. Проверить дату active standby через `Статус`.
2. Решить, допустим ли показанный возраст копии.
3. Выбрать `Запустить автономный режим`, тип `unplanned`.
4. Сообщить всем, что production нельзя использовать даже при кратком
   восстановлении связи.
5. Работать только в local до freeze/export.

Unplanned start не доказывает, что production перестал быть writer. Поэтому
failback допускается только после строгого production probe. Любое независимое
изменение даёт `CONFLICT`.

## Работа offline

На каждой основной странице показывается заметный banner:

- `ЛОКАЛЬНАЯ РЕЗЕРВНАЯ КОПИЯ` означает read-only standby без сессии;
- `АВТОНОМНЫЙ РЕЖИМ` означает активную local write session;
- frozen banner означает, что новые складские записи заблокированы.

Offline доступны поиск, остатки, поступления, перемещения, продажи, списания,
инвентаризация, Cell Recount, резервы, отчёты, клиенты, ремонты и scanner
workflows. ИИ-поддержка показывает `Недоступно в автономном режиме` до сетевого
вызова и не зависает.

Global write guard проверяет каждую business SQL mutation. На PostgreSQL
shared transaction advisory lock удерживается от проверки state до завершения
транзакции с mutation. Start,
freeze, maintenance probe и finalizer используют exclusive lock. HTTP write
request дополнительно выполняется в одной transaction с shared lock.

## Завершение и export

В меню выбрать `Завершить автономную работу` и ввести
`ЗАВЕРШИТЬ-И-ЗАМОРОЗИТЬ`.

1. Exclusive lock дожидается текущих write transactions.
2. Deployment переходит в `emergency_frozen`.
3. Все последующие business writes блокируются.
4. Создаются DB dump и media archive.
5. Проверяются dump, archive и SHA-256.
6. Manifest получает final state и offline lineage.
7. Session становится `FROZEN`.

Если export сломался, session остаётся `EXPORT_FAILED`, а DB остаётся frozen.
После устранения причины используется `emergency_stop --resume` с той же
confirmation phrase. Повторная запись не открывается.

## Failback check

Перед check production обязательно перевести в maintenance. Probe доступен
только в production mode по HTTPS и только с exact header token. Probe берёт
exclusive advisory lock и возвращает consistent read-only marker.

В меню выбрать `Проверить возможность возврата на сервер`.

`ELIGIBLE` возможен только если одновременно совпали:

- production mode и maintenance write lock;
- database identity и общий base ancestor;
- app commit и migration fingerprint;
- production business generation;
- полный business SHA-256 и markers всех business tables;
- production media tree SHA-256;
- local final dump/media hashes;
- local final lineage и instance identity.

Результаты:

- `ELIGIBLE` - automatic overwrite всё равно выключен; можно готовить change
  window после independent review;
- `CONFLICT` - доказано независимое изменение production business data или
  media, overwrite запрещён;
- `BLOCKED` - identity/schema/code/probe/package не позволяют доказать safety.

Report содержит base, production и local high-level markers и изменившиеся
tables. Чувствительные значения не логируются.

Probe не следует HTTP redirects, принимает только pinned root HTTPS URL и
сравнивает production instance identity с source instance base backup.

## Conflict и reconciliation

При `CONFLICT`:

1. Не отключать production backup retention.
2. Не удалять local final export и package.
3. Не запускать `restore_db` или `restore_media` на production.
4. Сохранить production safety backup под maintenance.
5. Передать package и report разработчику/ответственному за reconciliation.
6. Сравнить документы, движения, продажи, inventories, reservations и media.
7. Применять согласованные изменения только штатными business operations.

Первая версия не объединяет две PostgreSQL базы и не выбирает победителя
автоматически.

## Подготовка failback package

Пункт меню `Подготовить пакет` доступен после failback check. Он создаёт ZIP и
отдельный `.sha256` в `.emergency/packages/`. В package находятся только final
manifest, dump, media archive при наличии и failback report. Upload и restore
не выполняются.

Package со статусом `CONFLICT` или `BLOCKED` допустим только для
reconciliation. Production finalizer принимает только package с `ELIGIBLE`.

## Controlled production restore procedure

Эта процедура не автоматизирована и не выполняется операторским меню. Нужны
approved change window, fresh production backup, независимый review package и
план rollback.

1. Убедиться, что failback report имеет `ELIGIBLE`.
2. Оставить production в maintenance.
3. Создать дополнительный full production safety backup и проверить offsite.
4. Проверить package SHA-256 на production host.
5. Распаковать package в отдельный каталог внутри `backups/`.
6. Остановить `web` и `proxy`, оставив PostgreSQL доступным только локальному
   Docker network.
7. Восстановить DB dump штатным `restore_db --yes`.
8. Восстановить media как exact snapshot, а не поверх более нового дерева.
   Старое media сохранить как rollback volume/snapshot. Простое overlay-copy
   недостаточно, потому что удалённые offline файлы останутся лишними.
9. Не открывать proxy и не разрешать writes.
10. Запустить migrations/checks на exact app commit из package.
11. Выполнить production finalizer:

    ```bash
    docker compose exec web python manage.py production_finalize_failback \
      --package /app/backups/failback/failback-SESSION.zip \
      --sha256 EXPECTED_SHA256 \
      --confirm 'ОПАСНО-ПРИНЯТЬ-FAILBACK'
    ```

12. Finalizer повторно проверяет package, app, migrations, database identity,
    business SHA-256 и media SHA-256. Только затем state становится `normal`.
13. Запустить smoke tests, открыть proxy и проверить `/healthz/`.
14. В local меню выбрать `Подтвердить завершённый возврат`. Local lifecycle
    очищается только если probe подтверждает accepted session и exact final
    fingerprints.

Важно: команда finalizer не выполняет restore. До шага 11 restored production
остаётся fail-closed в `emergency_frozen`.

## Rollback

Если DB restore, media restore, check или finalizer не прошли:

1. Не открывать production writes.
2. Остановить web/proxy.
3. Вернуть pre-restore production DB backup.
4. Вернуть сохранённый production media snapshot.
5. Запустить migrations/checks на прежнем release.
6. Проверить data/media markers и health.
7. Открыть прежний production только отдельным maintenance disable после
   документированного решения.
8. Сохранить local package и report для reconciliation.

## Recovery после прерывания

- Прерванный standby restore не активирует candidate.
- `starting` marker без DB session требует status/diagnostics и явного
  `emergency_start --resume` только после проверки active DB.
- `ACTIVE` переживает перезапуск Windows: `control.json` и DB session остаются.
- `FREEZING` требует явного `emergency_stop --resume`.
- `EXPORT_FAILED` остаётся frozen и recoverable.
- Повреждённый final dump делает failback `BLOCKED`.
- Повреждённый `control.json` блокирует lifecycle вместо выбора DB наугад.

## Retention

Автоматически сохраняются active standby и минимум одна previous verified
standby. Downloads ограничивает `DENSTOCK_EMERGENCY_KEEP_DOWNLOADS`. Final
exports и packages не удаляются до `COMPLETED`.

Пункт `Удалить старые подтверждённые копии` требует exact phrase и удаляет
только артефакты старых `COMPLETED` sessions. Минимум один completed export
всегда сохраняется; число задаёт
`DENSTOCK_EMERGENCY_KEEP_COMPLETED_EXPORTS`.

## Audit и диагностика

DB audit хранит standby sync, start, freeze, export, failback check, package,
conflict, completion и retention. Windows log находится в
`.emergency/logs/emergency-control.log`. Пароли и tokens не записываются.

Основные команды диагностики:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\operations\DenisStock-Emergency.ps1 -Action Status

docker compose --project-name denstock-emergency `
  --env-file .env.emergency -f docker-compose.emergency.yml ps
```

Удалять PostgreSQL volume или `.emergency` при незавершённой session запрещено.

## Известные ограничения первой версии

- Автоматический merge production и local отсутствует намеренно.
- Advisory lock защищает writes через DenisStock. Прямой SQL доступ отдельными
  credentials должен быть организационно закрыт; если это нельзя доказать,
  failback остаётся `BLOCKED` или требует reconciliation.
- Manifest защищает целостность SHA-256, но не является цифровой подписью.
  Доверие к источнику обеспечивают доступ к bucket/rclone и независимая проверка.
- Production restore и exact media swap остаются отдельной ручной процедурой с
  backup, review и rollback plan.

## Известный пробел прогона на PostgreSQL

Тест `tests/test_ai_support_files.py::test_private_attachment_ownership_headers_and_missing_file`
падает ТОЛЬКО на PostgreSQL. Он закрывает `FileResponse`, а закрытие ответа
поднимает `request_finished` и закрывает соединение с БД; на PostgreSQL это
рвёт транзакцию теста, и последующие обращения к ORM внутри того же теста
падают с `the connection is closed`. На SQLite в памяти Django соединение не
закрывает, поэтому там тест проходит.

Это дефект изоляции ТЕСТА, а не продукта: закрывать соединения на конце
запроса правильно.

Почему не исправлено этой ночью: перевод теста на транзакционную БД чинит его
на PostgreSQL, но ломает идущие следом миграционные тесты возвратов
(`django_content_type` создаётся повторно). Закрыть только файловый дескриптор
не удалось: `response.file_to_stream` в этом окружении пуст. Правильное
решение требует отдельной аккуратной работы с порядком транзакционных тестов и
выходит за рамки аварийного режима, поэтому пробел зафиксирован, а не закрыт
наспех.
