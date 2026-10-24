# Отдельный выпуск healthcheck публичного каталога

Этот runbook относится к исправлению HTTP `Host` в `docker/http_healthcheck.py`.
Он не разрешает установку. Точный SHA и SHA-256 квалифицированного архива
берутся из отчёта о локальной сборке и отдельного разрешения владельца.

## Причина и текущая смешанная версия

После выпуска `741b65c686624dc5a2f5ec5629019af924e13407` внутренний `web`
работает на образе `sha256:77f381cdcbbaef846c30971f6a720b4a1043516ede5a178b9f396037f75c1a47`.
`catalog-web` вернули на прежний образ
`sha256:b941abf118173a6881bd8eb8f9d2c967fb72eda72408fffac1f128b329c438d7`.
Оба сервиса healthy. Защищённый rollback overlay каталога включён через
`COMPOSE_FILE` в `.env`; его не удалять.

Предыдущий probe подключался к `127.0.0.1` и отправлял такой же HTTP `Host`.
Публичный Django принимал `Host: localhost` и отклонял `Host: 127.0.0.1`
с HTTP 400. Исправленный probe оставляет TCP-соединение на loopback, но
передаёт `Host: localhost`. Таймауты, прямой Docker `CMD` и проверка ровно
HTTP 200 не меняются. У кандидата нет миграций или изменений бизнес-логики.

## PRE и STOP-гейты

1. Получить отдельное разрешение владельца и тихое окно. Подтвердить текущие
   image ID обоих сервисов, health, оба HTTPS-сайта, анонимный 404 приватных
   вложений, PostgreSQL, ботов, MAX webhook, Telegram offset, RAM, swap, диск,
   OOM, backup-lock, миграции и контрольные показатели бизнес-данных.
2. Зафиксировать текущий `COMPOSE_FILE`, `DENSTOCK_BUILD_REVISION`, хеши
   Compose overlays, image ID и фактические healthcheck обоих сервисов.
   Скопировать изменяемую `.env` только в защищённый каталог VPS с правами
   `0700/0600`; не печатать её содержимое или секреты.
3. Создать новый полный подписанный PRE под общим backup-lock через
   `backup_all` без `--keep-last` и capped wrapper. Проверить `db.dump`,
   `media.tar.gz`, `private_media.tar.gz`, Ed25519, SHA-256, затем зашифровать
   age и сохранить на Mac. Восстановить PostgreSQL 16 и оба дерева файлов
   в изоляции, проверить ссылки. Старые копии и Yandex не трогать.
4. Перед установкой сравнить production с подписанным PRE по политике A/B/C
   из квалифицированного release runbook. Новые необъяснённые изменения
   остатков, документов, сообщений, схемы, проблемы доставки, backup или
   здоровья сервиса означают STOP. Не восстанавливать PRE поверх production.

## Доставка и catalog-only установка после отдельного разрешения

1. На Mac сверить полный Git SHA кандидата, `git archive` SHA-256, OCI
   revision, image ID, платформу `linux/amd64`, файл probe и SHA-256 Docker
   архива. Сборку выполнять с `docker buildx build --pull --platform linux/amd64`
   из точного Git archive. Перед сохранением release-архива сверить версию
   Python в базовом образе с `FROM` в Dockerfile: локальный тег может указывать
   на другую версию и не доказывает происхождение образа. Передать архив в
   новый защищённый каталог VPS без сборки там.
   Сверить SHA-256 на VPS и выполнить `docker image load`. Сверить immutable
   image ID и оба label с точным Git SHA и архивом исходников. Присвоить
   только тег `denstock-catalog-web:<новый SHA>`.
2. Оставить production checkout и глобальный `DENSTOCK_BUILD_REVISION` на
   `741b65c...`: они описывают работающий `web` и не должны подменяться ради
   каталога. Создать защищённый Compose overlay только для `catalog-web`:

   ```yaml
   services:
     catalog-web:
       image: denstock-catalog-web:<новый SHA>
       environment:
         DENSTOCK_APP_COMMIT: <новый SHA>
   ```

   В `COMPOSE_FILE` заменить **только** последний путь к существующему
   `catalog-rollback.yml` на путь к новому overlay. Прежний файл сохранить.
   Docker healthcheck остаётся прямым `CMD` из базового Compose.
3. До пересоздания разобрать `docker compose --profile public-catalog config
   --format json` локальным фильтром, не печатая секреты. Сравнить полную
   конфигурацию каждого сервиса до/после: менять разрешено только image,
   `DENSTOCK_APP_COMMIT` и healthcheck каталога. У `web`, ботов, PostgreSQL
   и Caddy конфигурация обязана совпасть. Проверить, что старые image ID
   сохранены. Постоянный `COMPOSE_FILE` должен выбирать тот же overlay и после
   перезагрузки VPS; обычный `docker compose config` не должен возвращать
   каталог к старому или обновлять `web`.
4. Поскольку стандартный `release_image_identity.py verify` требует **один**
   глобальный SHA для всех сервисов, он не подтверждает смешанную версию.
   Для каталога отдельно сверить Git-объект кандидата, SHA-256 `git archive`,
   image labels, image environment, Compose image и runtime container image ID
   с утверждённым SHA. Для `web` независимо повторно сверить сохранённый
   image ID, OCI/source labels, runtime environment и прямой healthcheck
   с `741b65c...`. Не выдавать проверку checkout или `.env` за доказательство
   фактического кода контейнера.
5. Только после этих проверок выполнить
   `docker compose --profile public-catalog up -d --no-build --no-deps --wait catalog-web`.
   Не запускать `up` для других сервисов. Проверить OCI revision, image ID,
   runtime SHA, прямой Docker `CMD`, несколько успешных healthcheck, HTTP 200
   склада и каталога, 404 приватных вложений, PostgreSQL, ботов, отсутствие
   orphan/zombie, OOM и роста RAM/swap. Повторить контроль данных.
6. Создать отдельный подписанный POST без ротации, зашифровать на Mac и
   практически восстановить PostgreSQL 16, public media и private_media.
   До успешного восстановления POST статус не `SUCCESS`.

## Возврат только каталога

При ошибке остановить дальнейшие шаги и сохранить диагностику. После оценки
и разрешения владельца вернуть в `COMPOSE_FILE` прежний защищённый
`catalog-rollback.yml`. Он выбирает image ID
`sha256:b941abf118173a6881bd8eb8f9d2c967fb72eda72408fffac1f128b329c438d7`,
прежний HTTP check с `localhost` и прежний runtime SHA-selector каталога.
Сначала проверить результирующий Compose config, затем пересоздать только
`catalog-web` с `--no-build --no-deps --wait` и проверить оба сайта.
Не менять `web`, PostgreSQL, Caddy, ботов, схему и клиентские данные; не
восстанавливать старую БД автоматически. Предыдущие образы и PRE/POST сохранить.
