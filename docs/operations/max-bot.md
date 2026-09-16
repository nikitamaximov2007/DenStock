# MAX-бот заявок клиентов: выпуск и эксплуатация

Документ для инженера, который выполняет выпуск MAX V1 на production (Stage C),
и для администратора после него. Архитектура и гарантии домена описаны в
[docs/design/customer-request-max.md](../design/customer-request-max.md).
Реальные токены, секреты и ID сотрудников в этот файл не вписываются.

Точный порядок команд печатает сам инструмент выпуска, этот документ объясняет
каждый шаг и решения:

```
python3 scripts/operations/max_release.py plan --base <production SHA> --candidate <release SHA>
```

## 1. Архитектура

```
клиент PRO-STOR ──POST «Продолжить в MAX»──> catalog-web ──303──> https://max.ru/<bot>?start=<token>
MAX ──HTTPS 443 POST /customer-requests/max/webhook/──> proxy (pro-brp.ru, только этот путь и POST) ──> web
web: проверка X-Max-Bot-Api-Secret, запись в БД, ответ {"ok": true}
max-bot: очередь MaxMessage ──HTTPS (корень Минцифры)──> platform-api2.max.ru
max-bot: события MaxOutboxEvent ──> MaxOperatorDelivery (пользователи DenisStock)
telegram-bot: MaxOperatorDelivery ──> Telegram сотрудникам («Открыть заявку»)
сотрудник: ответ в карточке заявки DenisStock ──> MaxMessage ──> max-bot ──> клиенту от имени бота
```

## 2. Сервисы и зачем каждый участвует в выпуске

| Сервис | Что меняется | Почему пересоздаётся |
| --- | --- | --- |
| `web` | код webhook и формы ответа, миграции `customer_requests 0008, 0009` и `operations 0006` (их применяет entrypoint), чтение `.env.max-webhook` | без него нет схемы, webhook и ответа сотрудника |
| `telegram-bot` | код `send_max_operator_deliveries` | уведомления сотрудникам о MAX доставляет только он; старый код оставил бы их в очереди навсегда |
| `max-bot` | новый сервис | отправка клиентам MAX, рассылка событий сотрудникам, аренда и heartbeat |
| `catalog-web` | кнопка «Продолжить в MAX», CSP, `MAX_BOT_USERNAME` | вход клиента; пересоздаётся последним |
| `proxy` | только перечитывание Caddyfile (`caddy reload`), контейнер не пересоздаётся | маршрут webhook на `pro-brp.ru` |
| `db` | не трогается | миграции выполняет `web` |

## 3. Секреты: у каждого сервиса только своё

| Значение | max-bot | web | catalog-web | telegram-bot | db |
| --- | --- | --- | --- | --- | --- |
| `MAX_BOT_TOKEN` | да (`.env.max`) | нет, в compose принудительно пусто | нет, пусто (и в `public.py`) | нет, пусто | нет |
| `MAX_WEBHOOK_SECRET`, `MAX_WEBHOOK_ENABLED` | нет, пусто и `false` | да (`.env.max-webhook`) | нет, пусто | нет, пусто | нет |
| `MAX_API_CA_FILE`, `MAX_API_CA_SHA256` | да (`.env.max`) | нет | нет | нет | нет |
| `MAX_PUBLIC_WEBHOOK_URL` | да (`.env.max`) | нет | нет | нет | нет |
| `MAX_BOT_USERNAME` (публичное) | нет | нет | да (`.env.public`) | нет | нет |
| `TELEGRAM_BOT_TOKEN` | нет, пусто | нет | нет | да (`.env.telegram`) | нет |

Проверяется тестами `tests/test_max_bot_compose.py`.

## 4. Файлы на сервере

| Путь | Владелец, права | Содержимое | Кто создаёт |
| --- | --- | --- | --- |
| `/opt/denstock/.env.max` | root, 600 | `MAX_BOT_TOKEN`, `MAX_PUBLIC_WEBHOOK_URL`, `MAX_API_CA_FILE`, `MAX_API_CA_SHA256` | `max_release.py install-secrets` |
| `/opt/denstock/.env.max-webhook` | root, 600 | `MAX_WEBHOOK_ENABLED=true`, `MAX_WEBHOOK_SECRET` (сгенерирован, 64 символа) | `max_release.py install-secrets` |
| `/etc/denstock/max/russian-trusted-root-ca.pem` | root, 644 | публичный корневой сертификат Минцифры | инженер вручную |
| `/etc/denstock/caddy/Caddyfile` | root, 640 | маршрутизация proxy (не файл из репозитория) | `max_release.py edge-install` |
| `.env.public` | root, 600 | + `MAX_BOT_USERNAME` | `max_release.py set-username` |

`.env` (общий для web, telegram-bot, max-bot) MAX-значений не получает;
`preflight` останавливает выпуск, если `MAX_BOT_TOKEN` или `MAX_WEBHOOK_SECRET`
оказались в `.env`.

## 5. Сертификат MAX: корень Минцифры

Проверка с production (Stage C0, только чтение): `platform-api2.max.ru`
доступен напрямую, но его сертификат выдан «Russian Trusted Sub CA», корень
«Russian Trusted Root CA» Министерства цифрового развития. В стандартных
хранилищах доверия его нет: TLS не проходит ни на хосте, ни в контейнерах.

Решение: доверие только внутри клиента MAX. Проверка сертификата не
отключается, системное хранилище и остальные клиенты не меняются, прокси не
используется.

1. Скачать корневой сертификат из официального источника Минцифры / Госуслуг
   (раздел о российских сертификатах безопасности), формат PEM.
2. Сверить SHA-256 с официально опубликованным значением:
   `openssl x509 -in russian-trusted-root-ca.pem -noout -fingerprint -sha256`.
3. Положить в `/etc/denstock/max/russian-trusted-root-ca.pem` (root, 644).
4. Тот же SHA-256 (64 hex) передать в `install-secrets --ca-sha256`.
   Клиент откажется работать, если файл заменят.

## 6. Токен существующего бота

Бот PRO-STOR в MAX уже создан владельцем. Второй не создаётся. Токен не
вставляется в чат, в историю shell и в Git.

```
python3 /root/max_release.py install-secrets --ca-sha256 <sha256> --execute
```

Токен вводится дважды в скрытом режиме (getpass). Сначала инструмент проверяет
сертификат, потом спрашивает токен. Файлы создаются с правами 600, значения не
печатаются. Повторная запись только с `--replace` (ротация).

## 7. Проверка бота: GET /me

```
docker compose --profile max-bot run --rm --no-deps max-bot max_bot_identity
```

Печатает `is_bot: true`, `user_id`, `username`, deep-link без payload и SHA-256
доверенного сертификата. Токен и заголовки не печатаются. Ошибка сертификата
сопровождается подсказкой про `MAX_API_CA_FILE`. Выполняется до любых изменений
базы: при ошибке выпуск откатывается без шагов с БД.

## 8. Публичное имя бота для каталога

Имя не угадывается: оно берётся из ответа MAX.

```
python3 /root/max_release.py set-username "$(docker compose --profile max-bot run --rm --no-deps max-bot max_bot_identity --env-line)" --execute
```

Без `MAX_BOT_USERNAME` заявки создаются как обычно, страница успеха честно
пишет, что MAX недоступен, и менеджер звонит клиенту.

## 9. Маршрут webhook

Файл production `deploy/caddy/Caddyfile.production` отличается от
действующего (`Caddyfile.production.pre-max`, sha256 `ff363d12…`) только блоком
`pro-brp.ru`: `POST` ровно на `/customer-requests/max/webhook/` уходит в `web`
с заголовком `Host: 185-250-44-206.sslip.io` и лимитом тела 64 КБ. Всё
остальное на `pro-brp.ru` по-прежнему идёт только в `catalog-web`.

```
python3 scripts/qualification/max_edge_check.py deploy/caddy/Caddyfile.production   # локально, настоящий Caddy
python3 /root/max_release.py edge-install --execute
```

`edge-install` проверяет, что живой файл равен ожидаемому, сохраняет копию,
переписывает файл на месте (одиночный bind mount держит inode), убеждается,
что контейнер видит новый файл, выполняет `caddy validate` и `caddy reload`.
При любой ошибке возвращает прежний файл.

Проверка снаружи: `curl -s -o /dev/null -w '%{http_code}\n' -X POST
https://pro-brp.ru/customer-requests/max/webhook/` даёт 404 (нет секрета).

## 10. Подписка webhook

```
docker compose --profile max-bot run --rm --no-deps -v /opt/denstock/.env.max-webhook:/run/max-webhook.env:ro max-bot max_webhook subscribe --confirm --secret-file /run/max-webhook.env
docker compose --profile max-bot run --rm --no-deps max-bot max_webhook status --require-subscribed
```

`status` только читает. `subscribe` и `unsubscribe` требуют `--confirm`;
повтор безопасен. Секрет читается из файла web и не печатается. Чужие подписки
помечаются `(OTHER)` и не удаляются.

## 11. Здоровье

* `max-bot`: healthcheck `manage.py max_bot_health`: heartbeat-файл свежее
  90 секунд, в нём ID работающего экземпляра, аренда в БД принадлежит ему и не
  истекла. Не обращается к MAX и не читает секреты.
* `python3 /root/max_release.py verify --candidate <SHA> --services web,telegram-bot,max-bot,catalog-web --require-subscribed`:
  код выпуска в каждом сервисе, миграции, здоровье, подписка.

## 12. Порядок запуска и остановки

Запуск (см. `plan`): образы, сертификат и секреты, GET /me, `web` (миграции и
секрет webhook), `public-role`, `telegram-bot`, `max-bot`, `edge-install`,
`subscribe`, `set-username`, `catalog-web` последним.

Почему так: до миграций база не меняется, пока проверяется бот. Старые
`catalog-web` и `telegram-bot` работают на новой схеме, миграции только добавляют.
Уведомления MAX до обновления `telegram-bot` просто ждут. Без маршрута и
подписки MAX не может прислать обновление. Кнопка у клиентов появляется, только
когда всё за ней уже работает.

Остановка MAX (без выпуска): `max_webhook unsubscribe --confirm`, затем
`docker compose --profile max-bot stop max-bot`. Сообщения в очереди сохраняются
и уйдут после запуска; незавершённая отправка станет «Неизвестно, доставлено ли»
и не повторится.

## 13. Резервная копия PRE (первая запись на production)

Выпуск не начинается, если не выполнено всё:

* `/usr/local/sbin/denstock-backup-capped` (создание, подпись, offsite);
* `manage.py verify_backup <папка>`: PASS;
* подпись Ed25519 `production-1`: `verify_manifest` PASS;
* отрицательный контроль: изменённый манифест отклонён;
* `rclone check backups/<RUN> "$BACKUP_OFFSITE_TARGET/<RUN>"`: 0 differences;
* `app_commit` в манифесте равен SHA production до выпуска.

## 14. Приёмка на production

Все заявки явно тестовые, после проверки отменяются обычным переходом статуса.

### Новый клиент MAX

1. Отправить тестовую заявку PRO-STOR с выбором MAX.
2. «Продолжить в MAX», в MAX нажать «Начать».
3. Проверить сводку: номер заявки, позиции, цены на момент заявки.
4. Написать `первое тестовое сообщение`: ровно один ответ
   `Сообщение передано менеджеру PRO-STOR.`
5. Написать `второе тестовое сообщение`: второго подтверждения нет.
6. Сотрудник видит оба в Telegram и в карточке заявки.
7. Сотрудник отвечает в карточке: `тестовый ответ менеджера`.
8. Клиент получает его один раз от бота PRO-STOR.

### Возвращающийся клиент (тот же аккаунт MAX)

1. Вторая тестовая заявка, «Продолжить в MAX».
2. Записать, что пришло в существующий диалог: сообщение `/start <payload>`,
   `bot_started` или ничего (ответ бота «Готово. MAX подключён к заявке ...»
   означает привязку).
3. `/requests` или кнопки: выбрать заявку B, написать, сообщение только в B.
4. Выбрать A, написать, сообщение только в A.

Записать наблюдения: повторяется ли `callback_id` между нажатиями на одной
клавиатуре (видно по количеству подтверждений выбора), длины `mid`
(`max_forensics.py` печатает их), `Retry-After`, только если 429 случился сам.
Искусственно лимиты не провоцировать.

### Короткая регрессия Telegram

`telegram-bot` healthy; одна тестовая заявка подключается; первое сообщение
сохраняется и приходит сотрудникам; одно подтверждение; ответ сотрудника
доходит; зависших строк нет.

### Проверка данных

```
docker compose exec -T -e MAX_FORENSICS_REFERENCES=<A>,<B> -e MAX_FORENSICS_OPERATORS=<логины> web python manage.py shell < scripts/operations/max_forensics.py
```

Результат `MAX FORENSICS PASS`. `business_counts=` до и после приёмки совпадают
(продажи, строки, резервы, движения, приходы, ремонты, выдачи в ремонт,
списания, пересчёты, оплаты). `release_baseline.py` до и после выпуска
отличается только SHA.

## 15. Резервная копия POST

Только после PASS приёмки нового и возвращающегося клиента, ответа сотрудника,
отмены тестовых заявок, регрессии Telegram, чистых очередей и здоровых сервисов.
Те же требования, что для PRE, но `app_commit` равен SHA выпуска MAX.

## 16. main после приёмки

`main` переводится на принятый SHA выпуска только fast-forward, без force,
squash и rebase: история Stage A, B, C сохраняется.

## 17. Откат

Начинается всегда с прекращения трафика MAX:

1. `max_webhook unsubscribe --confirm`
2. `docker compose --profile max-bot stop max-bot`
3. `unset-username --execute` и пересоздание `catalog-web` (кнопка исчезает)
4. `edge-rollback --execute` (прежний Caddyfile, reload)
5. `.env.max-webhook` и `.env.max` убрать в `/root` (webhook отвечает 404 после
   пересоздания `web`)

Дальше решение:

* **A. Откат кода** (ошибка в поведении, здоровье, маршруте): `git checkout
  --detach <SHA до выпуска>`, `DENSTOCK_APP_COMMIT`, сборка и пересоздание `web`,
  `telegram-bot`, `catalog-web`. Миграции не откатываются: они только добавляют,
  код до выпуска работает на новой схеме (проверено репетицией). Скрипт прав
  публичной роли повторять не нужно.
* **B. Восстановление базы из PRE** только если повреждены бизнес-данные или
  миграция оставила схему непригодной. Остановить `web`, `catalog-web`,
  `telegram-bot`, восстановить подписанную PRE-копию по регламенту
  восстановления, затем A.

После отката: `release_baseline.py` совпадает с PRE, webhook на `pro-brp.ru`
отвечает от `catalog-web` (404), Telegram-заявка подключается.

## 18. Ротация

* Токен: `install-secrets --replace --execute` (webhook-секрет тоже новый), затем
  пересоздать `web` и `max-bot` и повторить `subscribe`.
* Только секрет webhook: тот же порядок; до `subscribe` MAX шлёт старый секрет
  и получает 404, поэтому окно держать коротким.

## 19. Локальная репетиция

```
python3 scripts/qualification/max_release_rehearsal.py --workdir <пустой каталог> --base <production SHA> --candidate <release SHA>
```

Весь выпуск на production compose с фальшивыми MAX и Telegram: от состояния до
MAX через все шаги `plan`, приёмку, forensics и откат.
