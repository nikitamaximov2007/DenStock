# Единый выпуск web: быстрые действия, внутренние номера, номер таможенного заказа

Документ описывает установку одной интеграционной версии `web` поверх
production `741b65c686624dc5a2f5ec5629019af924e13407`. Целевой SHA берётся из
отчёта о квалификации (это коммит, содержащий данный документ). Документ не
даёт разрешения менять production: установка выполняется только после
отдельного согласия владельца.

## Состав

Интеграционная ветка `claude/three-feature-prod-rc-20261029` содержит три уже
проверенные доработки, перенесённые cherry-pick на `741b65c` в таком порядке:

1. `ae8c1b0` быстрые действия: стрелки между весом брутто, весом нетто,
   областью применения и количеством, живой поиск клиента, защита от
   устаревших ответов поиска. Без миграций.
2. `b7b94f7`, `32dc7e3`, `2337408` внутренние справочные номера: строгий
   поиск по артикулу, заводскому штрихкоду и внутреннему номеру, защита от
   коллизий дефисов (`010-090` и `01-0090` разные карточки, `010090` при
   обеих требует выбора), одна строка пересчёта на карточку. Без миграций.
3. `9ef9f3f`, `71387d5` исправление номера таможенного заказа с историей и
   защитой от устаревшего сохранения. Миграция
   `customs_orders.0005_customsordernumberchange`.

Подробности каждой доработки: `docs/operations/internal-reference-release.md`,
`docs/operations/customs-order-number-edit-release.md`. Механика PRE/POST и
идентичности образа: `docs/operations/release-image-identity.md`. Уроки OOM:
`docs/operations/web-healthcheck-oom-release.md`.

Обновляется только `web`. PostgreSQL, `catalog-web`, Caddy, Telegram, MAX и
preview не пересобираются и не пересоздаются.

## Состояние production при подготовке (29.10.2026, только чтение)

- Checkout `/opt/denstock`: `741b65c`, неотслеживаемый только
  `docker-compose.signing.yml`.
- `COMPOSE_FILE`: `docker-compose.yml`,
  `deploy/ai-support/docker-compose.external.yml`, `docker-compose.signing.yml`,
  `/root/denstock-release-healthcheck-host-34a6545-20261024/catalog-only-proposed.yml`.
- Работающий `web`: image `sha256:77f381cdcbbaef846c30971f6a720b4a1043516ede5a178b9f396037f75c1a47`,
  OCI revision `741b65c`. Все сервисы healthy.
- `customs_orders` применена до `0004`, запланированных миграций нет.
- Память 2 ГиБ, доступно около 780 MiB, swap 512 MiB занят наполовину,
  OOM за 7 дней не было. Диск: свободно 4,6 ГБ (84 %), build cache Docker
  около 7,9 ГБ. Ничего не удалять в рамках выпуска.

Эти значения устаревают. Перед выпуском всё перепроверить.

## 1. Состояние VPS

Только чтение. Остановиться при любом отличии.

```bash
cd /opt/denstock
git rev-parse HEAD            # 741b65c686624dc5a2f5ec5629019af924e13407
git status --porcelain        # только ?? docker-compose.signing.yml
grep -E '^(COMPOSE_FILE|DENSTOCK_BUILD_REVISION)=' .env
docker compose ps
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/healthz/ || true
```

Проверить HTTPS склада и `pro-brp.ru`, анонимный 404 приватного вложения,
отсутствие активных складских операций и свободный общий backup-lock
(`/run/lock/denstock-backup.lock`), отсутствие идущего ночного backup.

## 2. RAM, swap, диск, Docker

```bash
free -m; swapon --show; cat /proc/pressure/memory
journalctl -k --since '-24 hours' | grep -ci 'out of memory\|oom-kill'
df -h /; df -i /
docker system df
ps -eo ppid,cmd | grep -c '[h]ttp_healthcheck.py'
```

Условия продолжения: новых OOM нет; `MemAvailable` не ниже 600 MiB; свободно
не меньше 2 ГБ на `/` (образ около 500 МБ, PRE и POST около 100 МБ каждый,
архив образа); число healthcheck-процессов не растёт. Иначе STOP.
Не собирать образ на VPS: сборка на 2 ГиБ после OOM-аварии запрещена.
Не чистить build cache и старые образы в рамках выпуска.

## 3. Подтверждение production SHA

```bash
python3 scripts/operations/release_image_identity.py verify \
  --sha 741b65c686624dc5a2f5ec5629019af924e13407 --services web --runtime
docker compose exec -T web python manage.py showmigrations customs_orders
docker compose exec -T web python manage.py migrate --plan
```

Ожидается `IMAGE IDENTITY PASS`, последняя миграция `0004`, план пуст.
Записать текущий web image ID: это точка возврата приложения.

```bash
OLD_WEB_IMAGE=$(docker inspect -f '{{.Image}}' "$(docker compose ps -q web)")
cp -a .env ".env.pre-three-feature-$(date -u +%Y%m%dT%H%M%SZ)"
```

## 4. Полный подписанный PRE

Только по `docs/operations/release-image-identity.md`, раздел PRE: общий
`flock`, `backup_all` без `--keep-last`, без capped wrapper, с
`DENSTOCK_APP_COMMIT=741b65c686624dc5a2f5ec5629019af924e13407`.

```bash
BASE_SHA=741b65c686624dc5a2f5ec5629019af924e13407
(
  exec 9>/run/lock/denstock-backup.lock
  flock -n 9 || exit 75
  docker compose exec -T -e DENSTOCK_APP_COMMIT="$BASE_SHA" web \
    python manage.py backup_all
)
```

Проверить `verify_backup` для созданного run ID: PostgreSQL dump,
`media.tar.gz`, `private_media`, manifest, SHA-256 и Ed25519 (`production-1`).
Загрузить именно этот run утверждённой неудаляющей процедурой copy и
read-back. Без `purge`, `sync`, `delete` и очистки bucket.

## 5. Восстановимость PRE

Перенести PRE на Mac, зашифровать age отдельно от прежних копий, сверить
SHA-256. Восстановить в изолированной PostgreSQL 16 на `127.0.0.1`:
`pg_restore` без ошибок, число таблиц и миграций, последняя
`customs_orders` равна `0004`, количественные маркеры (детали, остатки,
таможенные заказы и строки) совпадают с production, оба media-дерева
распаковываются, ссылки на файлы существуют. Временные данные удалить после
проверки. При неуспехе STOP: установка не начинается.

## 6. Образ и миграция 0005

Образ собран вне VPS, на Mac, из точного Git-архива целевого коммита, для
`linux/amd64`, с production overlay UID/GID 2001:

```bash
export DOCKER_DEFAULT_PLATFORM=linux/amd64
export COMPOSE_FILE=docker-compose.yml:deploy/ai-support/docker-compose.external.yml
export DENSTOCK_AI_CLIENT_GID=0   # только для разбора overlay при сборке
python3 scripts/operations/release_image_identity.py build --sha "$TARGET_SHA" --services web
docker save "denstock-web:$TARGET_SHA" | gzip -1 > "denstock-web-$TARGET_SHA.tar.gz"
shasum -a 256 "denstock-web-$TARGET_SHA.tar.gz"
```

После разрешения на передачу: скопировать архив на VPS, сверить SHA-256,
`docker load`, затем сверить image ID с отчётом о квалификации. Затем
обновить checkout без изменения неотслеживаемых файлов и селектор:

```bash
git fetch origin claude/three-feature-prod-rc-20261029
git checkout --detach "$TARGET_SHA"
sed -i '/^DENSTOCK_APP_COMMIT=/d' .env
sed -i '/^DENSTOCK_BUILD_REVISION=/d' .env
printf 'DENSTOCK_BUILD_REVISION=%s\n' "$TARGET_SHA" >> .env
python3 scripts/operations/release_image_identity.py verify --sha "$TARGET_SHA" --services web
docker compose config --images
```

В `docker compose config` по сравнению с прежним состоянием должны
измениться только `web.image`, SHA в окружении web и build args. Каталог
остаётся на образе из catalog-only overlay. Другие различия: STOP.

Миграцию применить отдельно, до пересоздания web, одноразовым процессом нового
образа. Entrypoint обходится, поэтому `collectstatic` не трогает статику,
которую сейчас отдаёт старый web:

```bash
docker compose run --rm --no-deps --entrypoint python web manage.py migrate --plan
#   ожидается ровно: customs_orders.0005_customsordernumberchange
docker compose run --rm --no-deps --entrypoint python web \
  manage.py migrate customs_orders 0005 --noinput
```

`0005` только создаёт таблицу истории и два индекса, существующие таблицы не
меняются. Старый web продолжает работать с новой таблицей: локально на
PostgreSQL 16 код `741b65c` прошёл `check`, `migrate` ответил «No migrations to
apply» и прочитал заказы. Перед `run` убедиться, что `MemAvailable` не ниже
600 MiB. При ошибке миграции STOP, сохранить журнал; web не пересоздавать.

## 7. Обновление только web

```bash
docker compose up -d --no-build --no-deps --wait web
python3 scripts/operations/release_image_identity.py verify \
  --sha "$TARGET_SHA" --services web --runtime
```

Entrypoint повторно выполнит `migrate` (ожидается «No migrations to apply») и
`collectstatic`. Не запускать `build` или `up` для `catalog-web`, ботов, proxy,
db и preview.

## 8. Здоровье приложения

`docker compose ps` (web healthy), `/healthz/` 200, HTTPS склада, журнал web без
трассировок, `showmigrations customs_orders` оканчивается на `0005`. Повторить
пункт 2: память, swap, OOM, число healthcheck-процессов. В течение 30 минут
наблюдения они не должны расти.

## 9. POST-проверки трёх функций

Без складских записей и без изменения номеров настоящих заказов.

- Быстрые действия: страница открывается без ошибок в консоли браузера,
  стрелки переводят фокус между весом брутто, нетто, областью и количеством
  в существующем черновике (значения не менять), поле «Найти клиента»
  показывает совпадения и не показывает архивных клиентов.
- Поиск и склад: общий поиск по известному артикулу и по внутреннему номеру
  (например, `127388` должен открыть `AT-04164`, если карточка есть в базе);
  страницы инвентаризации, приёмки, списания и перемещения открываются. Скан
  не проводить. Неоднозначный номер в поиске показывает список, а не одну
  карточку.
- Таможенные заказы: открыть существующий заказ, блок «История изменения
  номера» пуст, форма «Изменить номер» открывается и закрывается по «Отмена»,
  XLSX скачивается. Сверить `id`, число строк, курс и суммы с PRE.

Первую реальную правку номера проверяет уполномоченный сотрудник штатно:
старый и новый номер, автор и время в истории, неизменные `id`, состав, курс
и суммы.

## 10. Публичный каталог и остальные сервисы

`pro-brp.ru` 200, поиск каталога работает, `catalog-web`, `telegram-bot`,
`max-bot`, `proxy`, `db` не пересоздавались (сверить `Up` и container ID с
пунктом 1), анонимный 404 приватного вложения сохранён.

## 11. POST-backup

Тот же порядок, что в пункте 4, с `DENSTOCK_APP_COMMIT="$TARGET_SHA"`, без
ротации. Manifest должен назвать целевой SHA. Загрузка неудаляющей
процедурой с read-back, age-копия на Mac отдельно от PRE, изолированное
восстановление PostgreSQL 16 с последней `customs_orders` равной `0005` и
обоих media-деревьев. До успешного восстановления POST выпуск не закрыт.

## 12. Откат

Решение об откате принимает владелец. Не восстанавливать PRE поверх новых
бизнес-операций и не выполнять обратную миграцию без отдельного решения.

Возврат приложения (схема совместима):

```bash
git checkout --detach 741b65c686624dc5a2f5ec5629019af924e13407
sed -i '/^DENSTOCK_BUILD_REVISION=/d' .env
printf 'DENSTOCK_BUILD_REVISION=%s\n' 741b65c686624dc5a2f5ec5629019af924e13407 >> .env
python3 scripts/operations/release_image_identity.py verify \
  --sha 741b65c686624dc5a2f5ec5629019af924e13407 --services web
docker compose up -d --no-build --no-deps --wait web
python3 scripts/operations/release_image_identity.py verify \
  --sha 741b65c686624dc5a2f5ec5629019af924e13407 --services web --runtime
```

Образ `denstock-web:741b65c...` уже есть на VPS (image ID записан в пункте 3).
Старый entrypoint выполнит `migrate`: при применённой `0005` он ничего не
меняет. Таблицу `customs_orders_customsordernumberchange` **не удалять** и
`migrate customs_orders 0004` не выполнять: таблица может уже хранить
настоящие исправления номеров, а старый код её не использует. Исправленные
номера заказов после отката остаются исправленными; их история сохраняется
для повторной установки.

Восстановление PRE допустимо только при повреждении данных и только по
отдельному решению владельца, с предварительным отдельным backup текущего
состояния.
