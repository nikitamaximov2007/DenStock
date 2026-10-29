# Независимые выпуски web, каталога и ботов

Документ описывает раздельные селекторы образов и порядок, в котором каждое
приложение (`web`, `catalog-web`, `telegram-bot`, `max-bot`) выпускается и
откатывается независимо. Документ не даёт разрешения менять production: каждое
применение выполняется только после отдельного согласия владельца.

## Причина

До этой ветки все четыре сервиса брали тег образа, build arg и
`DENSTOCK_APP_COMMIT` из одного `DENSTOCK_BUILD_REVISION`. Выпуск `web`
29.10.2026 перевёл общий селектор на `01a0b7e`, и Compose стал разрешать для
ботов `denstock-telegram-bot:01a0b7e...` и `denstock-max-bot:01a0b7e...`,
которых не существует. Работающие боты не пострадали, потому что их не
пересоздавали, но любое `up` бота упёрлось бы в отсутствующий образ.

## Новые правила

- У каждого сервиса свой селектор: `DENSTOCK_WEB_REVISION`,
  `DENSTOCK_CATALOG_WEB_REVISION`, `DENSTOCK_TELEGRAM_BOT_REVISION`,
  `DENSTOCK_MAX_BOT_REVISION`. Значение: полный SHA коммита, из которого собран
  образ. Выпуск сервиса меняет только его строку в `.env`.
- Общий `DENSTOCK_BUILD_REVISION` остаётся запасным значением для локальной
  разработки. В production `.env` его быть не должно: `release_image_identity.py
  verify` отклоняет его и требует все четыре собственных селектора.
- `release_image_identity.py selectors` (только чтение) показывает для каждого
  сервиса селектор, образ, который разрешает Compose, есть ли он на машине,
  атрибутирован ли он (OCI revision равен селектору) и использует ли его
  работающий контейнер. Код выхода 1, если образа нет или overlay подменяет
  селектор.
- Каждый сервис получает весь `.env` через `env_file`, поэтому публичные
  значения селекторов видны во всех контейнерах. Образ, `DENSTOCK_APP_COMMIT`,
  build args и остальная конфигурация других сервисов при выпуске одного не
  меняются (тест `test_compose_render_moves_only_the_service_whose_selector_changed`).
  Но из-за изменения `.env` команда `docker compose up -d` без имени сервиса
  перезапустила бы остальные контейнеры. Поэтому всегда указывать сервис и
  `--no-deps`: `docker compose --profile max-bot up -d --no-build --no-deps --wait max-bot`.

## Применение в production (после отдельного разрешения)

Состояние на 29.10.2026 (только чтение):

| Сервис | Работающий образ | Код | Атрибуция |
|---|---|---|---|
| web | `78f2ddb2…` (`denstock-web:01a0b7e…`) | `01a0b7e` | есть |
| catalog-web | `061d4ea5…` (`denstock-catalog-web:34a6545…`) | `34a6545` | есть |
| telegram-bot | `33e655e9…` (`denstock-telegram-bot:latest`) | `e359488` | нет, образ от 17.10 |
| max-bot | `37a700b9…` (`denstock-max-bot:latest`) | `e359488` | нет, образ от 17.10 |

Код ботов определён сравнением git blob SHA всех 823 файлов `apps`, `config`,
`templates`, `docker`, `manage.py`, `requirements.txt` в контейнере с деревом
коммита: с `e35948804069b3fe8ffa3f499fefe0a775541b47` различий 0, с `01a0b7e`
различий 70.

1. Только чтение: `git rev-parse HEAD`, `docker compose ps`, image ID каждого
   контейнера, `free -m`, отсутствие идущего backup.
2. Сохранить `.env`: `cp -a .env .env.pre-selectors-$(date -u +%Y%m%dT%H%M%SZ)`.
3. Дать образам ботов тег их **доказанного** коммита. Это не новая сборка и не
   изменение контейнеров; тег совпадает с кодом внутри образа:

   ```bash
   docker image tag 33e655e9a8e5 denstock-telegram-bot:e35948804069b3fe8ffa3f499fefe0a775541b47
   docker image tag 37a700b90605 denstock-max-bot:e35948804069b3fe8ffa3f499fefe0a775541b47
   ```

   Атрибуции у этих образов нет, `selectors` покажет `LEGACY-UNATTESTED`.
   Первый независимый выпуск каждого бота заменит его атрибутированным образом.
4. В `.env` удалить общий селектор и записать четыре собственных:

   ```bash
   sed -i '/^DENSTOCK_BUILD_REVISION=/d' .env
   printf 'DENSTOCK_WEB_REVISION=%s\n' 01a0b7eb4fb79b25bc6cbc7ae87ce4da8eb1bad6 >> .env
   printf 'DENSTOCK_CATALOG_WEB_REVISION=%s\n' 34a65455f610694b383d400426a9e2d08fa6124f >> .env
   printf 'DENSTOCK_TELEGRAM_BOT_REVISION=%s\n' e35948804069b3fe8ffa3f499fefe0a775541b47 >> .env
   printf 'DENSTOCK_MAX_BOT_REVISION=%s\n' e35948804069b3fe8ffa3f499fefe0a775541b47 >> .env
   ```

5. Обновить checkout до коммита этой ветки, когда он войдёт в выпуск, либо
   применить вместе с ближайшим выпуском web: новый `docker-compose.yml` и
   `release_image_identity.py` являются частью кода.
6. Проверить без запуска контейнеров:

   ```bash
   python3 scripts/operations/release_image_identity.py selectors
   docker compose --profile public-catalog --profile telegram-bot --profile max-bot config --images
   ```

   Ожидается: все четыре образа существуют, web и каталог attested, боты
   `LEGACY-UNATTESTED`, у всех `running=same`. Контейнеры не пересоздаются:
   ни одна команда `up` в этом шаге не выполняется.
7. Возврат: восстановить сохранённый `.env`. Теги ботов безвредны и остаются.

catalog-only overlay (`/root/denstock-release-healthcheck-host-34a6545-20261024/catalog-only-proposed.yml`)
по-прежнему явно закрепляет образ каталога. После шага 4 он совпадает с
`DENSTOCK_CATALOG_WEB_REVISION`. Убрать overlay можно отдельным решением; до
этого `selectors` должен показывать `OK` и для каталога.

## Выпуск одного бота

1. Ветка от актуального production-коммита, разработка, тесты (`pytest`,
   ruff, djlint, check, makemigrations --check), PostgreSQL 16 для затронутых
   модулей, локальная проверка с фейковыми API (`tests.max_fake`,
   `tests.telegram_fake`).
2. Если у бота меняются миграции, это уже выпуск web: миграции применяет только
   web, боты без них не стартуют.
3. Сборка вне VPS, `linux/amd64`, тем же инструментом:

   ```bash
   COMPOSE_PROJECT_NAME=denstock DOCKER_DEFAULT_PLATFORM=linux/amd64 \
     python3 scripts/operations/release_image_identity.py build --sha "$SHA" --services max-bot
   ```

   Передача архивом `docker save | gzip`, сверка SHA-256, `docker load`, сверка
   image ID на VPS.
4. PRE-backup по `release-image-identity.md` (lock, `backup_all` без ротации,
   `verify_backup`, read-back). Бот пишет в те же таблицы, что web.
5. Сохранить текущий образ бота тегом
   `denstock-max-bot:rollback-<дата>` и строку селектора.
6. Изменить только `DENSTOCK_MAX_BOT_REVISION`, затем
   `release_image_identity.py verify --sha "$SHA" --services max-bot`.
7. `docker compose --profile max-bot up -d --no-build --no-deps --wait max-bot`,
   затем `verify ... --services max-bot --runtime` и `selectors`.
8. POST: health, lease и heartbeat, `last_error` пуст, журнал без трассировок,
   доставка тестового ответа только в служебный чат сотрудника, без
   сообщений реальным клиентам. Для Telegram: тот же порядок с профилем
   `telegram-bot`.
9. POST-backup при изменении схемы или данных; при выпуске только кода бота
   достаточно PRE.
10. Откат: вернуть строку селектора, `docker compose --profile max-bot up -d
    --no-build --no-deps --wait max-bot`. Старый образ хранится до следующего
    успешного выпуска.
