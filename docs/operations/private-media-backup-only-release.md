# Выпуск backup-only: private_media в каждом production-бэкапе

> Устарело: актуальный план на production-линии в
> `private-media-backup-release-plan.md`.

Статус: кандидат подготовлен и проверен на синтетических данных.
**Деплой НЕ выполнялся.** Production (сервер, Docker, база, Yandex S3, rclone)
в ходе подготовки не затрагивался. Перед выпуском нужен независимый аудит.

## 1. Кандидат

- Ветка: `claude/private-media-backup-only-rc`.
- База: `origin/main` = `bc53d8956fd6d588b8317e968a2392dfc94b3c50`
  (проверено `git fetch origin` 2026-10-12; совпадает с production).
- Итоговый SHA кандидата: последний коммит ветки; сверять командой
  `git ls-remote origin claude/private-media-backup-only-rc`. Этот документ
  входит в последний коммит, поэтому собственный SHA в нём не записан.
- Миграций нет (`makemigrations --check` чистый). Схема БД не меняется.

Изменённый код относительно `main`:

| Файл | Что меняется |
|---|---|
| `apps/operations/private_media.py` (новый) | проверка дерева, архив, проверка архива, восстановление с откатом |
| `apps/operations/backup.py` | `backup_all` пишет `private_media.tar.gz` и поля manifest; каталог рана не переиспользуется; полная проверка дампа PostgreSQL |
| `apps/operations/emergency_manifest.py` | проверка полей private_media в manifest |
| `apps/operations/restore.py` | `verify_backup` проверяет подпись Ed25519 и архив; web-restore восстанавливает private_media первым шагом |
| `apps/operations/management/commands/restore_private_media.py` (новый) | ручное восстановление приватного тома |
| `apps/operations/failback.py` | failback-пакет переносит архив, если он есть в manifest |
| `apps/operations/views.py`, `templates/operations/backups.html` | пометка «без приватных файлов», столбец «Приватные» |
| `apps/customer_requests/attachments.py` | ссылка на вложение убирается в БД до удаления файла |

## 2. Что взято из Round 3 и что исключено

Источник: `codex/audit-private-media-backup` @ `a0d7760` (= `main` + 7 коммитов).
Перенос только через `git cherry-pick -x`, без merge.

| Коммит | Решение | Причина |
|---|---|---|
| `ed3016b` архив private_media, manifest, Ed25519, restore | ВЗЯТ целиком | ядро выпуска: создание, проверка, восстановление |
| `19ea7d8` hardening | ВЗЯТ ЧАСТИЧНО | см. ниже |
| `a1e4794` подпись emergency failback | ИСКЛЮЧЁН | аварийный failback и новые ключи станции: отдельный выпуск offline DR |
| `a70d599` запрет online restore в production | ИСКЛЮЧЁН | это шлюз offline cutover; web-restore закрыт флагом `DENSTOCK_ENABLE_WEB_RESTORE` (по умолчанию `false`, значение в production проверить), новый формат не делает его опаснее, чем публичные media |
| `dd4b1f7` новая обёртка `denstock-backup-capped-safe.sh` | ИСКЛЮЧЁН | владелец запретил менять обёртку в этом выпуске; установка обёртки это отдельный выпуск |
| `6b7aa10` документация offline cutover | ИСКЛЮЧЁН | не относится к backup-only |
| `a0d7760` тесты обёртки | ИСКЛЮЧЁН | тестирует исключённую обёртку |

Из `19ea7d8` взято:

- каталог рана создаётся с `exist_ok=False`: два запуска в одну секунду не
  смешиваются в одном поколении (имена каталогов прежние);
- `verify_database_payload` декодирует весь дамп в `/dev/null`: проверка
  только оглавления (`--list`) пропускала повреждённые блоки данных;
- в production `verify_backup` отклоняет неподписанный manifest: удаление
  подписи не превращает новую копию в «старую»;
- `cleanup_attachment` сначала убирает ссылку в БД, файл удаляется после
  commit: блокировка бэкапа не застанет ссылку на уже удалённый файл.

Из `19ea7d8` исключено:

- обязательный каталог публичных media и сверка публичного архива до и после
  упаковки: новый вид отказа без повторной попытки; под `set -e` обёртки он
  остановил бы ежедневную выгрузку, а к private_media отношения не имеет;
- проверка подписи и хэшей при локальной ротации: меняет поведение ротации
  под действующей обёрткой и перечитывает все 14 копий каждую ночь;
- развёртывание private_media на аварийной станции и правка
  `docker-compose.emergency.yml`: часть offline DR.

Собственные изменения этого кандидата:

- повторная попытка снимка, если бот переписал файл короче во время упаковки
  (раньше бэкап падал с «unexpected end of data» вместо повтора);
- пометка копий без приватных файлов в интерфейсе «Бэкапы»;
- PostgreSQL-тесты аудита переведены на `serialized_rollback`, как остальные:
  без этого они падали в зависимости от порядка запуска.

## 3. Совместимость с действующей обёрткой

Обёртка `/usr/local/sbin/denstock-backup-capped` (в репозитории её нет) не
меняется. Проверено тестами:

- имя каталога рана прежнее, `ГГГГ-ММ-ДД_ЧЧ-ММ-СС`;
- `backup_all --keep-last 14` не начинает падать на каталогах, которые нельзя
  удалить (8 каталогов root от 2026-09-21): ротация как и раньше молча их
  пропускает (`rmtree(ignore_errors=True)` не менялся);
- неудачный бэкап не вытесняет последнюю проверенную копию;
- новый файл в ране всего один: `private_media.tar.gz`; `.partial` в
  завершённом ране не остаётся.

Новые причины ненулевого кода `backup_all` (обёртка остановится и в этот день
ничего не выгрузит, это намеренно: неполная копия не должна считаться полной):
нет каталога private_media, нет прав на чтение файла или каталога, ссылка или
специальный файл, остатки `.denstock-restore-*`, дерево менялось три попытки
подряд, нет места на диске, каталог рана с таким именем уже есть.

## 4. Compose и env

- `docker-compose.yml`: изменений не нужно. `web` уже монтирует
  `private_media:/app/private_media` и `./backups:/app/backups`; боты
  монтируют тот же том.
- `.env`, `.env.backup`: изменений не нужно. `BACKUP_REMOTE_SOFT_LIMIT_BYTES`
  в этом выпуске НЕ меняется.
- Проверка подписи в `verify_backup` требует в `web` настроенных
  `DENSTOCK_MANIFEST_PUBLIC_KEY_PATH` и `DENSTOCK_MANIFEST_SIGNING_KEY_ID`.
  Если обёртка вызывает `verify_backup` и ключа нет, обёртка остановится.
  Это нужно проверить ДО деплоя (раздел 6).

## 5. Объём, мягкий лимит, версионированный бакет

Оценка от 2026-10-09: приватные файлы 16 шт., 51,3 MB, в архиве 45,9 MB.
Копия растёт с ~46,5 MB до ~92,4 MB.

- Облако: при действующем `BACKUP_REMOTE_SOFT_LIMIT_BYTES` (800 MiB) в
  облаке помещается примерно 6-9 копий вместо прежнего вдвое большего числа. Бакет версионирован,
  обёртка чистит скрытые версии `cleanup-hidden`, а Yandex обновляет учёт
  размера с задержкой до 20 минут. Утром после релиза (ежедневная копия плюс
  PRE и POST) возможен выход за лимит бакета 921,6 MB; лимит бакета взят из
  комментария обёртки и с сервера не проверялся.
- **Решение владельца** (не входит в этот выпуск): оставить 800 MiB и
  принять риск отказа выгрузки в день релиза, либо отдельным изменением
  поставить `734003200` (700 MiB, 4-6 копий) до или сразу после выпуска.
- Локальный диск: 14 копий по ~92,4 MB это ~1,3 GB плюс 8 неудаляемых
  каталогов. Перед деплоем проверить `df -h /opt/denstock`.
- Блокировка записи на время бэкапа становится дольше на время упаковки
  ~51 MB (секунды, ночью).

## 6. Перед деплоем (только чтение, выполняет оператор)

1. `git fetch origin`, `git rev-parse origin/main`: если `main` ушёл вперёд,
   кандидат нужно пересобрать на новом `main`.
2. Прочитать обёртку: `sudo cat /usr/local/sbin/denstock-backup-capped`.
   Убедиться, что она выгружает каталог рана целиком (`rclone copy <ран>`),
   а не список файлов. Если список: `private_media.tar.gz` не уйдёт в облако,
   выпуск блокируется до решения владельца.
3. Узнать, вызывает ли обёртка `verify_backup`. Проверить ключ в `web`:
   `docker compose exec -T web python manage.py shell -c "from pathlib import Path; from django.conf import settings as s; print(bool(s.DENSTOCK_MANIFEST_SIGNING_KEY_ID), Path(s.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH or '/nonexistent').is_file())" </dev/null`
   Ожидается `True True`.
4. Права UID 2001 на приватный том (ничего не меняет):
   `docker compose exec -T -u 2001:2001 web find /app/private_media ! -readable </dev/null`
   Вывод должен быть пустым. Иначе `backup_all` будет падать каждую ночь.
5. Нет ссылок, спецфайлов и жёстких ссылок:
   `docker compose exec -T web find /app/private_media ! -type f ! -type d </dev/null`
   и `... find /app/private_media -type f -links +1` пустые.
6. `df -h /opt/denstock`: свободно не меньше 2 GB.

## 7. Деплой (описание, не выполнялся)

1. PRE backup действующей обёрткой, `verify_backup <ран>`, `rclone check` с
   «0 differences».
2. `git checkout <SHA кандидата>`, обновить `DENSTOCK_APP_COMMIT` в `.env`.
3. Пересобрать `web` и `catalog-web` без затрагивания БД:
   `docker compose --profile public-catalog up -d --build --no-deps --wait web catalog-web`.
4. Рекомендуется в том же окне пересобрать ботов (изменение удаления вложений
   работает в них): `docker compose --profile telegram-bot --profile max-bot up -d --build --no-deps telegram-bot max-bot`.
   Если отложить, бэкап всё равно полный; остаётся только узкая гонка, где
   копия БД может ссылаться на уже доставленное и удалённое вложение.
5. Миграций нет; PostgreSQL не перезапускается.

## 8. Приёмка после деплоя

1. POST backup действующей обёрткой.
2. В новом ране есть `private_media.tar.gz`; в manifest
   `private_media_status = included`, `private_media_file_count` равен числу
   файлов в томе (`docker compose exec -T web find /app/private_media -type f | wc -l`).
3. `docker compose exec -T web python manage.py verify_backup <ран>`: строка
   `[OK] Подпись manifest (Ed25519)` и `[OK] Содержимое private_media совпадает с manifest`.
4. Отрицательный контроль подписи на копии рана во временном каталоге
   (изменить одно поле manifest): `verify_backup` обязан отказать.
5. Offsite: `rclone check backups/<ран> "$BACKUP_OFFSITE_TARGET/<ран>"`,
   ожидается «0 differences».
6. Независимое восстановление (вне production, на машине владельца):
   скачать ран из облака, `verify_backup`, затем восстановить в НОВУЮ базу и
   НОВЫЕ каталоги, сравнить с manifest. На чистом PostgreSQL до `pg_restore`
   создать роль: `CREATE ROLE denstock_public NOLOGIN;` (дамп содержит GRANT
   этой роли). Архив private_media уже имеет корень `.`: перекладывать
   `_data/`, как в ручном снимке 2026-10-11, не нужно.
7. Две строки вложений до сих пор ссылаются на файлы в ПУБЛИЧНЫХ media
   (запасной путь хранилища). Эти байты лежат в `media.tar.gz`, а не в
   `private_media.tar.gz`; после восстановления обоих архивов они читаются.

## 9. Откат

- Только код: `git checkout bc53d89...`, пересобрать те же сервисы. Данные и
  схема не менялись.
- Проверено: код `main` (`bc53d89`) читает копии нового формата.
  `verify_manifest`, `validate_manifest` и `verify_backup` старого кода
  принимают подписанную копию с private_media; архив просто игнорируется.
- Копии, сделанные новым кодом, остаются в облаке и пригодны после отката.

## 10. Проверка кандидата (2026-10-12, синтетические данные)

| Прогон | main `bc53d89` | кандидат |
|---|---|---|
| Полный SQLite-набор | 6365 passed, 7 failed, 272 skipped | 6428 passed, 7 failed, 277 skipped |
| PostgreSQL 16, операции, бэкап, вложения | 216 passed, 0 failed, 39 skipped (11 файлов) | 265 passed, 0 failed, 56 skipped (17 файлов) |

Все 7 падений унаследованы и совпадают поимённо (прочерк вместо цены x3,
кнопка отчёта partial-repair, ai_support_renderer, max_bot_compose,
max_edge_route). Падений только у кандидата нет. Пропуски на PostgreSQL:
SQLite-фикстуры и сценарии Windows PowerShell.

Гейты: `ruff check .`, `manage.py check`, `makemigrations --check --dry-run`,
`djlint templates --check`, `git diff --check`: чисто. Shell-скрипты не
менялись.

Вне проверки: живая обёртка, реальный бакет Yandex, UID 2001 и ключ подписи
на сервере (раздел 6), реальные данные.
