# План реализации — Слой 25. Локальная эксплуатация, backup и подготовка к использованию

**Статус:** УТВЕРЖДЁН (2026-06-30) · все решения зафиксированы жёстко (§ниже) · реализация строго в границах §19. **Ключевой инвариант приёмки: эксплуатационный слой. Делает проект безопаснее в эксплуатации, но НЕ меняет складскую бизнес-логику — никаких новых продаж, движений, отчётов, фото-логики, scanner-флоу; не создаёт `StockMovement`, не меняет `StockBalance`. Если backup/ops-команда создаёт движение или меняет остаток — это ошибка слоя.**

**Финальный слой дорожной карты.** Не трогаем существующий djlint-долг в старых шаблонах (не связан с этим слоем).

---

## 1. Цель слоя

Подготовить систему к **безопасному локальному использованию** у Дениса: понятный
запуск, проверка здоровья/готовности, **резервное копирование БД и media**, документированное
восстановление, обслуживание media-файлов и базовая эксплуатационная документация. Это
**слой надёжной эксплуатации**, а не новый складской модуль.

**Особое внимание:** после Слоя 24 появились **пользовательские файлы в `mediafiles/`**.
Слой 25 обязан (а) обеспечить **сохранность media** в локальном/compose-сценарии и (б)
включить media в **backup**.

### Что уже есть (фактическое состояние, проверено)

| Артефакт | Состояние |
|---|---|
| `docker-compose.yml` | `db` (postgres:16, том `pgdata`), `web` (без media-тома), `proxy` (caddy). **media-тома нет** |
| `docker/entrypoint.sh` | ждёт БД → `migrate` → `collectstatic` → опц. `createsuperuser` |
| `docker/caddy/Caddyfile` | `reverse_proxy web:8000` + gzip. **`/media/` НЕ обслуживается** |
| `apps/core/views.healthz` | `GET /healthz/` → app + `SELECT 1`; 200/503 (lightweight) |
| `config/settings` | `prod.py` `DEBUG=False`; `MEDIA_ROOT=BASE_DIR/mediafiles`, `MEDIA_URL=/media/` |
| `config/urls.py` | media раздаётся **только при `DEBUG`** (Слой 24) → в compose (DEBUG=False) media не отдаётся |
| `.env.example` | Django/Postgres/superuser/Caddy переменные; **без BACKUP_ROOT** |
| `.gitignore` | `db.sqlite3`, `/staticfiles/`, `/mediafiles/`, `/media/`; **без `/backups/`** |
| management-команды | паттерн есть (`check_stock_balance`, `rebuild_stock_balance`, …) |
| `README.md` | статус устарел («Текущий слой: 1 — каркас»); backup описан как «будущее» |

> **Выявленный пробел (важно):** в compose `DEBUG=False`, а Caddy не отдаёт `/media/` →
> **загруженные в Слое 24 фото в боевом compose-режиме не отображаются** и теряются при
> пересборке (нет тома). Слой 25 это закрывает (§13, §14).

---

## 2. Что нужно для локальной эксплуатации (состав слоя)

- Актуальный **README** (запуск, superuser, backup, restore, media, обновление, troubleshooting).
- Актуальный **`.env.example`** (+ опц. `BACKUP_ROOT`).
- **docker-compose**: том для media + bind для backups; (проверка `pgdata`).
- **Caddy**: отдача `/media/*` (чтобы фото показывались в compose).
- **healthcheck** — оставить лёгким (app+DB); тяжёлые проверки — в `ops_check`.
- **backup**: `backup_db`, `backup_media`, `backup_all`.
- **restore**: `restore_db`, `restore_media` (под `--yes`).
- **обслуживание**: `ops_check` (готовность к эксплуатации), опц. retention.
- **troubleshooting** в README.

---

## 3. Текущее состояние — выводы по проверке

- **Dockerfile/entrypoint:** образ `python:3.12-slim`, `psycopg[binary]`. **`pg_dump` в
  образе НЕТ** (binary-psycopg тянет libpq, но не клиентские утилиты) → для `pg_dump`
  нужно либо добавить `postgresql-client` в образ, либо вызывать из сервиса `db` (§5).
- **Caddy:** только reverse-proxy; `/static/` отдаёт whitenoise внутри `web`; `/media/`
  не отдаётся никем при DEBUG=False (§14).
- **healthz:** app + DB; этого достаточно для контейнерного healthcheck.
- **settings:** `connection.settings_dict` уже содержит разобранный `DATABASE_URL`
  (`ENGINE/NAME/USER/PASSWORD/HOST/PORT`) — используем его в backup (не парсим URL руками).

---

## 4. Где размещаем — **рекомендация: новое `apps/operations` (без моделей)**

| Вариант | Вердикт |
|---|---|
| Новое `apps/operations` с management-командами, **без моделей** | ✅ **выбран**: чистая группировка ~6 ops-команд, не засоряет домен; нет моделей → нет миграций |
| Команды в `apps/core/management/commands` | ⚠️ допустимо, но смешивает эксплуатацию с доменным core |
| Shell-скрипты | ⚠️ менее переносимо (Windows у Дениса), хуже тестируется |

`apps/operations` добавляем в `INSTALLED_APPS`. **Без моделей/миграций.** Логику пишем как
**тестируемые функции** в `apps/operations/backup.py` (принимают пути/параметры), а
management-команды — тонкие обёртки. Это позволяет тестировать backup на временных файлах,
не завися от боевой БД.

---

## 5. Backup БД (`backup_db`)

**Engine-aware** (чтобы dev/SQLite и prod/PostgreSQL оба работали):

- **PostgreSQL** (`ENGINE…postgresql`): `pg_dump` в **custom-формате** (`-Fc`) →
  `backups/<timestamp>/db.dump`. Параметры берём из `connection.settings_dict`; пароль
  передаём через **`PGPASSWORD` в окружении** процесса (НЕ в argv — не светим в `ps`/логах).
  Если `pg_dump` недоступен — **понятная ошибка** `CommandError` («установите postgresql-client
  или запустите backup из сервиса db»).
- **SQLite** (`ENGINE…sqlite3`): копия файла `NAME` → `backups/<timestamp>/db.sqlite3`.
  Если `NAME == ":memory:"` (тесты) — понятное сообщение, без падения.
- Проверяет окружение (какой engine), создаёт каталог `backups/<timestamp>/`, печатает путь.

**Рекомендация по pg_dump в Docker:** добавить `postgresql-client` в `docker/Dockerfile`
(небольшой пакет), чтобы `docker compose exec web python manage.py backup_all` работал
«из коробки». Версия клиента совместима с postgres:16.

---

## 6. Backup media (`backup_media`)

- Архивируем `MEDIA_ROOT` (`mediafiles/`) в **`backups/<timestamp>/media.tar.gz`** средствами
  **stdlib `tarfile`** (без внешних зависимостей).
- Сохраняем **рядом** с дампом БД (один timestamped-каталог).
- Если `mediafiles/` **отсутствует или пуст** — НЕ падаем: пишем понятное сообщение
  («media-файлов нет — нечего архивировать») и выходим успешно (нет фото — не ошибка).

---

## 7. Restore (`restore_db`, `restore_media`)

- **Обязательный флаг `--yes`**; без него — отказ с крупным предупреждением, что restore
  **перезапишет** текущие данные (exit без изменений).
- `restore_media --yes <archive>`: распаковать `media.tar.gz` в `MEDIA_ROOT` (перезапись).
- `restore_db --yes <dump>`:
  - **SQLite**: заменить файл БД копией из бэкапа (с предварительным предупреждением).
  - **PostgreSQL**: `pg_restore --clean --if-exists` (или `psql < db.sql`) в текущую БД;
    `PGPASSWORD` через окружение; понятная ошибка, если клиент недоступен.
- **Принцип безопасности:** опасное действие невозможно без явного `--yes`. Если для
  PostgreSQL полноценный авто-restore окажется хрупким — оставляем **минимальный
  документированный сценарий** в README (ручной `pg_restore`) и команду для **media**; для БД
  команда тогда печатает инструкцию вместо выполнения. Финальное решение — §«Решения».

---

### 7a. Манифест бэкапа (`backup_all` → `manifest.json`)

`backup_all` пишет в каталог рана **`manifest.json`** с метаданными: дата/время, engine
(postgres/sqlite), относительные пути артефактов (`db.*`, `media.tar.gz`), версия проекта и,
если доступен, текущий git-commit (через `git rev-parse --short HEAD`, мягко — отсутствие git
не ошибка). Секретов в манифест **не пишем** (ни пароля, ни DSN с паролем). Это помогает при
restore понять, что и когда сохранялось.

---

## 8. Retention — **рекомендация: простой `--keep-last N` на `backup_all` (иначе future)**

- Опциональный флаг `--keep-last N` у `backup_all`: после успешного бэкапа удалить самые
  старые каталоги `backups/*`, оставив N последних. По умолчанию — **хранить всё** (без
  удаления). Несколько строк, практично, не раздувает.
- Шифрование/ротация по объёму/расписание — **не делаем** (§19).

---

## 9. Healthcheck и `ops_check`

- **`/healthz/` не трогаем** (app + DB, 200/503) — он должен оставаться лёгким для частого
  опроса Docker/мониторингом.
- **Новая команда `ops_check`** (on-demand, не в healthz): проверяет готовность к
  эксплуатации и печатает чек-лист, ненулевой код при критичных проблемах:
  - **БД доступна** (`SELECT 1`);
  - **`MEDIA_ROOT`** существует (или может быть создан) и **доступен на запись** (пробная
    запись/удаление временного файла);
  - **`BACKUP_ROOT`** существует (или может быть создан) и **доступен на запись**;
  - **`pg_dump`/`pg_restore` доступны** в PATH — если engine PostgreSQL (для SQLite —
    неприменимо, отмечаем как «skip»);
  - базовые настройки media заданы (`MEDIA_URL`/`MEDIA_ROOT`);
  - `DEBUG=False` в прод-настройках; `SECRET_KEY` не равен небезопасному дефолту;
  - `ALLOWED_HOSTS` задан (не пуст/не `*` в проде — предупреждение).
  - Каждая проверка — с **понятным сообщением**; критичные (БД/писабельность/недоступный
    pg_dump при Postgres) → ненулевой exit; мягкие (DEBUG/hosts в dev) → предупреждение.

**Обоснование (почему media-write не в healthz, §9-запрос):** запись на диск при каждом
healthcheck (раз в 15 с по compose) — лишняя нагрузка и риск «флаппинга» при временной
блокировке ФС. Поэтому writability проверяем **по требованию** в `ops_check`, а healthz
оставляем лёгким (только факт «приложение живо + БД отвечает»).

---

## 10. Документация (README)

Обновить README (он устарел — стоит «Слой 1»):

- локальный запуск (Docker compose и без Docker/SQLite);
- `docker compose up -d --build`;
- создание superuser (авто из `.env` + ручной);
- вход в систему;
- **backup** (`backup_all` / `backup_db` / `backup_media`), где лежат архивы;
- **restore** (с предупреждением и `--yes`), документированный ручной сценарий для БД;
- где лежат **media** (`mediafiles/`, том в compose);
- что **не коммитить** (`.env`, `mediafiles/`, `backups/`, `db.sqlite3`);
- как **обновлять** проект (`git pull` → `docker compose up -d --build` → миграции авто в entrypoint);
- **troubleshooting** (БД не поднимается, фото не видны, порт занят, забыл пароль admin, бэкап/restore).

---

## 11. `.env.example`

- Проверить актуальность (Django/Postgres/superuser/Caddy — уже есть).
- Добавить **опц. `BACKUP_ROOT`** (по умолчанию `backups/` в корне проекта), если вводим
  настраиваемый путь. `MEDIA_ROOT` менять не нужно (значение из base.py устраивает; в compose
  путь покрывается томом).
- **Секреты не хранить** — только плейсхолдеры (как сейчас).

---

## 12. .gitignore

- Уже игнорируются: `mediafiles/`, `media/`, `staticfiles/`, `db.sqlite3`.
- **Добавить `/backups/`** — бэкапы содержат коммерческие/персональные данные и не должны
  попадать в git.

---

## 13. Docker — сохранность данных

- **Postgres:** том `pgdata` уже есть — не трогаем.
- **Media:** добавить **именованный том `media:`** на `/app/mediafiles` у сервиса `web` —
  иначе фото Слоя 24 теряются при пересборке. **Это обязательная часть слоя** (сохранность
  media решаем именно здесь).
- **Backups:** **bind-mount `./backups:/app/backups`** у `web` — чтобы Денис видел архивы в
  папке проекта на хосте (удобно копировать/уносить). Каталог `backups/` — в `.gitignore`.
- Текущий compose не ломаем (добавления аккуратные).

---

## 14. Caddy — отдача media

- Сейчас `/media/` в compose не отдаётся (DEBUG=False; Django media-route только в DEBUG;
  whitenoise — только static). Значит **фото в боевом локальном режиме не видны**.
- **Рекомендация (в этом слое):** добавить в Caddyfile отдачу статичных файлов media:
  ```
  handle_path /media/* {
      root * /srv/media
      file_server
  }
  ```
  и примонтировать **тот же media-том read-only** в контейнер `proxy` на `/srv/media`. Это
  3 строки и делает фото видимыми в compose. TLS/CDN/облако — вне слоя.
- Если решим не трогать Caddy — **явно** задокументировать в README ограничение «фото видны
  только в dev-режиме (`runserver`, DEBUG)». Но рекомендуем сделать (иначе Слой 24 в реальном
  локальном использовании не работает).

---

## 15. Безопасность

- **Бэкапы содержат коммерческие/персональные данные** → `/backups/` в `.gitignore`; не
  выкладывать; рекомендуется хранить копии вне рабочей машины (ручной шаг в README).
- **Пароли не светим:** `pg_dump`/`pg_restore` получают пароль через **`PGPASSWORD` в env**,
  не через argv; команды **не логируют** секреты (печатаем хост/БД/путь, но не пароль).
- **Без UI-загрузки/скачивания бэкапов** (§16).
- Права на каталог `backups/` — на усмотрение ОС (рекомендация в README не шарить папку).

---

## 16. UI — **не делаем; только CLI/management + документация**

**Обоснование (почему UI backup/restore опаснее):**
- кнопка «скачать бэкап» в вебе — это **канал утечки** всей БД (полный дамп через браузер,
  доступный по сессии/уязвимости);
- кнопка «restore» в вебе — **перезапись всех данных одним кликом** (риск случайной/злонамеренной
  потери);
- CLI/management-команды требуют **доступа к серверу/контейнеру** — это естественный барьер,
  бэкап/restore выполняет тот, у кого и так есть админ-доступ к машине.

Поэтому backup/restore — **только CLI**; в UI ничего не добавляем.

---

## 17. Права

- UI нет → **новые capabilities не нужны** (команды защищены доступом к серверу/контейнеру,
  как и существующие `rebuild_stock_balance`/`check_stock_balance`).
- Никаких изменений в `roles.py`.

---

## 18. Тесты (`tests/test_operations.py`)

Тестируем **функции** из `apps/operations/backup.py` на временных каталогах (`tmp_path`) и
команды через `call_command`:

1. `backup_media` создаёт `media.tar.gz` в целевом каталоге (из временного `MEDIA_ROOT` с файлом).
2. `backup_media` при **отсутствующей/пустой** media-папке — понятное сообщение, **без падения**.
3. `backup_db` (SQLite, временный файл-БД) делает копию; путь существует.
4. `backup_db` (Postgres-ветка) формирует корректную команду `pg_dump` и **не логирует пароль**;
   при недоступном `pg_dump` — **`CommandError` с понятным текстом**.
5. `backup_all` создаёт понятную структуру `backups/<ts>/` (db + media + `manifest.json`).
6. `manifest.json` содержит дату/engine/пути и **не содержит секретов**.
7. `restore_db`/`restore_media` **без `--yes`** → отказ (`CommandError`), данные не тронуты.
8. `restore_media --yes` восстанавливает архив в media root (round-trip с `backup_media`).
9. `--keep-last N` (если реализуем) оставляет N последних каталогов.
10. `ops_check` проверяет **media root** и **backup root** (создание/писабельность).
11. `ops_check` даёт **понятную ошибку**, если `pg_dump` недоступен при Postgres.
12. `.env.example` содержит обязательные переменные (`DJANGO_SECRET_KEY`, `DATABASE_URL`,
    `POSTGRES_*`, `DJANGO_SUPERUSER_*`).
13. `.gitignore` содержит `mediafiles`/`media`/`backups`/`db.sqlite3`.
14. `docker-compose.yml` содержит тома `pgdata` и `media` (и bind-mount на backups).
15. `Caddyfile` содержит маршрут `/media/`.
16. `/healthz/` по-прежнему отвечает 200 (регресс).
17. **Read-only для склада:** запуск ops/backup-команд **не создаёт** `StockMovement` и **не
    меняет** `StockBalance` (эксплуатация не трогает физику).
18. `makemigrations --check` — чисто (моделей не добавляли).

---

## 19. Чего НЕ делаем (границы Слоя 25)

Не реализуем: UI backup/restore; scheduled/cron-бэкапы; облачный backup/S3; шифрование и
key-management; мониторинг Prometheus/Grafana; полноценную DevOps-платформу; изменение
складской бизнес-логики; создание `StockMovement`; изменение `StockBalance`/продаж/отчётов/
фото-логики/scanner.

---

## 20. Ручная проверка

1. `docker compose up -d --build` → открыть `http://localhost`, войти администратором.
2. Загрузить фото детали (Слой 24) → **фото отображается** (Caddy отдаёт `/media/`).
3. `docker compose exec web python manage.py ops_check` → все проверки зелёные.
4. `docker compose exec web python manage.py backup_all` → в `./backups/<ts>/` появились
   `db.dump` и `media.tar.gz` (видны на хосте через bind-mount).
5. `docker compose down && docker compose up -d` → данные и **фото на месте** (тома сохранили).
6. Тест restore (на тестовой копии): `restore_media --yes <archive>` возвращает media; без
   `--yes` — отказ с предупреждением.
7. `git status` — `backups/`, `mediafiles/`, `.env` не попадают в индекс.
8. Проверить, что склад не изменился: `check_stock_balance` зелёный; движений не прибавилось.

---

## 21. Критерии готовности

1. `backup_db`/`backup_media`/`backup_all` работают (Postgres и SQLite), архивы в
   `backups/<ts>/`; media-бэкап не падает без файлов.
2. `restore_db`/`restore_media` требуют `--yes`; без него — безопасный отказ; есть
   документированный сценарий восстановления.
3. `ops_check` проверяет DEBUG/SECRET_KEY/БД/писабельность media/ALLOWED_HOSTS; healthz
   остался лёгким и рабочим.
4. **Media сохраняются** между пересборками (том) и **отдаются** в compose (Caddy `/media/`).
5. `.gitignore` исключает `backups/`; `.env.example` актуален; README описывает запуск/
   backup/restore/обновление/troubleshooting.
6. Границы §19 соблюдены; складская логика не изменена (read-only тест §18.16).
7. `pytest`/`ruff`/`djlint --check`/`manage.py check` зелёные; `makemigrations --check` —
   изменений нет (моделей не добавляли).

---

## 22. Файлы (создаются/изменяются)

**Создаются:**
- `apps/operations/__init__.py`, `apps/operations/apps.py`, `apps/operations/backup.py`
  (тестируемые функции), `apps/operations/checks.py` (для `ops_check`).
- `apps/operations/management/commands/{backup_db,backup_media,backup_all,restore_db,restore_media,ops_check}.py`.
- `tests/test_operations.py`.

**Изменяются:**
- `config/settings/base.py` — `"apps.operations"` в `INSTALLED_APPS` (+ опц. `BACKUP_ROOT`).
- `docker-compose.yml` — том `media:` на `web`, bind `./backups:/app/backups`, монтирование
  media в `proxy` (read-only).
- `docker/caddy/Caddyfile` — отдача `/media/*`.
- `docker/Dockerfile` — `postgresql-client` (для `pg_dump`/`pg_restore`).
- `.env.example` — опц. `BACKUP_ROOT`; актуализация комментариев.
- `.gitignore` — `/backups/`.
- `README.md` — раздел эксплуатации (запуск/backup/restore/media/обновление/troubleshooting),
  обновить устаревший статус.

**Без изменений:** складские модели/логика/миграции; `roles.py`; UI/шаблоны склада;
`/healthz/` (логика не меняется).

---

## 23. Что будет закоммичено

Два коммита (как в Слоях 5–24):
1. `План Слоя 25: локальная эксплуатация и резервное копирование` — этот файл (push в `origin/main`).
2. `Слой 25: локальная эксплуатация и резервное копирование` — реализация (после `pytest`,
   `ruff`, `djlint --check`, `makemigrations --check`, `manage.py check`), затем push в `origin/main`.

Это **финальный слой дорожной карты** — после него проект = первая production-ready версия
для локального использования (склад + отчёты + фото/этикетки + страховка данных).

---

## Решения (утверждены 2026-06-30)

Все границы зафиксированы заказчиком жёстко. Открытых вопросов нет.

1. **Размещение:** новое `apps/operations` **без моделей и миграций**; логика — тестируемые
   функции + тонкие команды — ✅ принято.
2. **Главная проблема закрывается сейчас:** media-том + Caddy `/media/*` + backup media (после
   Слоя 24 фото иначе ломаются/теряются в compose) — ✅ принято.
3. **Backup БД:** `pg_dump -Fc` для Postgres (+ **`postgresql-client` в Dockerfile**), копия
   файла для SQLite; пароль через **`PGPASSWORD`**, секреты не логируем; `backups/<ts>/` — ✅ принято.
4. **Backup media:** `tarfile` (stdlib) `media.tar.gz` рядом с дампом; отсутствие/пустота
   media — понятное сообщение, не падение — ✅ принято.
5. **Backup all:** один каталог рана с db + media + `manifest.json` (дата/engine/пути/commit,
   без секретов) — ✅ принято.
6. **Restore:** `restore_db`/`restore_media` **только под `--yes`** с предупреждением о
   перезаписи; Postgres — `pg_restore`, если безопасно/просто, иначе документированный ручной
   сценарий; **без подтверждения restore не делаем никогда** — ✅ принято.
7. **Retention:** `--keep-last N` на `backup_all`, если просто; иначе future — ✅ принято.
8. **Healthcheck:** `/healthz/` не перегружаем (app+DB), без записи в media; прод/писабельность/
   доступность `pg_dump` — в новом `ops_check` — ✅ принято.
9. **Docker:** том `media:` + bind `./backups:/app/backups` + `postgresql-client`; `pgdata` не
   ломаем; текущий compose не ломаем — ✅ принято.
10. **Caddy:** безопасная отдача `/media/*` (media-том read-only в `proxy`); static не ломаем — ✅ принято.
11. **UI/права:** UI backup/restore **не делаем**; новых capabilities нет (UI нет → права не
    нужны; CLI = барьер доступа к серверу) — ✅ принято.
12. **Документация/конфиги:** README актуализируем (запуск/superuser/media/backup/restore/
    ops_check/обновление/troubleshooting/«не коммитить backups/media»); `.env.example` (+опц.
    `BACKUP_ROOT`); `.gitignore` (`/backups/`) — ✅ принято.
13. **Security:** не логировать пароль БД, не писать секреты в backup-вывод/манифест; без
    download из веба; без cloud upload; `/backups/` не в git — ✅ принято.
14. **Инвариант:** ops/backup не создаёт `StockMovement`, не меняет `StockBalance`/документы/
    статусы/остатки/scanner/barcodes/бизнес-логику — ✅ принято.
