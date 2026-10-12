# Готовность к восстановлению DenisStock без VPS (2026-10-12)

Документ фиксирует, что из production уже есть в зашифрованном аварийном
комплекте на MacBook владельца, что проверено фактически и чего не хватает
для запуска DenisStock на новом сервере. Значения секретов здесь не
приводятся и не должны приводиться.

Статусы: VERIFIED (проверено расшифровкой и восстановлением), AVAILABLE
(сохранено и сверено по SHA-256 с источником, расшифровка ещё не проверена),
REISSUABLE (можно выпустить заново), MISSING (в комплекте нет),
NOT TESTED (не проверялось).

## Комплект

Каталог `~/DenisStock-Recovery/` на MacBook владельца. Шифрование age,
получатель `age1j9ru52...`, ключ `keys/recovery-identity.age` защищён
парольной фразой (Apple Passwords: «DenisStock — Emergency Recovery»).

| Архив | Содержимое |
|---|---|
| `data/2026-10-11_03-00-12.signed-backup.tar.age` | db.dump, media.tar.gz, подписанный manifest.json |
| `data/private_media.2026-10-11T17-40-49Z.tar.age` | 16 приватных файлов (снят отдельно, корень `_data/`) |
| `data/source-code.bc53d89.bundle.age` | Git bundle, полная история до bc53d89 |
| `data/production-config-nonsecret.2026-10-11.tar.age` | Caddyfile, systemd units, wrapper, compose override, публичные ключи |
| `secrets/production-secrets-2026-10-11T19-29-20Z.tar.age` | 17 файлов секретов и конфигурации (перечень ниже) |

## Матрица готовности

| Компонент | Статус | Примечание |
|---|---|---|
| Исходный код | VERIFIED | bundle проверен `git bundle verify`, есть и на GitHub |
| PostgreSQL backup | VERIFIED | восстановлен в PG16, отпечаток данных равен подписанному |
| Публичные media | VERIFIED | 12 файлов, дерево совпадает с manifest |
| private_media | VERIFIED | 16 файлов, SHA-256 каждого; снимок на ~17,5 ч позже базы |
| Подпись Ed25519 бэкапа | VERIFIED | ключ production-1, закреплённый публичный ключ |
| `.env` (web, боты) | AVAILABLE | Django secret, пароль БД, флаги |
| `.env.public` (витрина) | AVAILABLE | отдельный Django secret, URL роли `denstock_public` |
| `.env.telegram` | AVAILABLE, REISSUABLE | токен через BotFather; перевыпуск отключит старый |
| `.env.max`, `.env.max-webhook` | AVAILABLE, REISSUABLE | токен и секрет вебхука MAX |
| `.env.backup` | AVAILABLE | не секрет: цель offsite, retention, soft limit |
| Закрытый ключ подписи manifest | AVAILABLE, REISSUABLE | новый ключ потребует перезакрепления на станциях |
| `docker-compose.signing.yml` | AVAILABLE | неотслеживаемый override, обязателен |
| Caddyfile | AVAILABLE | хосты и маршруты production |
| CA для MAX API | AVAILABLE | публичный сертификат |
| `rclone.conf` (Yandex S3) | AVAILABLE, REISSUABLE | ключи доступа к бакету |
| Конфигурация egress-прокси Telegram и ИИ | AVAILABLE | `/etc/denstock-ai/*`, бинарник sing-box скачивается заново |
| systemd units, backup wrapper | AVAILABLE | в несекретном архиве конфигурации и шаблонах репозитория |
| TLS-сертификаты Caddy | REISSUABLE | Let's Encrypt выпустит при правильном DNS |
| Авторизация Codex для ИИ-поддержки | MISSING, REISSUABLE | сознательно не копировалась; повторный вход |
| Прайс-файлы `brp.xlsx`, `import/polaris.xlsx` | MISSING | только на VPS; результат импорта уже в базе |
| Прошлые локальные бэкапы, docker-образы | MISSING | не нужны: образы собираются из кода |
| Полный запуск сервиса на новом VPS | NOT TESTED | данные проверены, запуск нет |

## Что зависит от адреса 185.250.44.206

Внутренний хост `185-250-44-206.sslip.io` производный от IP. На новом
сервере с другим IP меняются: `CADDY_SITE_ADDRESS`, `DJANGO_ALLOWED_HOSTS`,
`DJANGO_CSRF_TRUSTED_ORIGINS`, `TELEGRAM_INTERNAL_BASE_URL`,
`MAX_PUBLIC_WEBHOOK_URL` и маршрут вебхука MAX в Caddyfile. Для
`pro-brp.ru` нужно перенаправить DNS. Это решение владельца.

## Изолированный первый запуск

Восстановленное окружение сначала поднимается без связи с внешним миром,
чтобы боты не ответили клиентам и вебхуки не ушли наружу.

1. Расшифровать комплект на новом сервере (или Mac) в каталог с правами 700.
2. Восстановить базу командой `dr_stage_restore` (ветка
   `claude/dr-offline-restore-round4`), создав роль `denstock_public NOLOGIN`
   до `pg_restore` и перепаковав архив private_media без префикса `_data/`.
3. Положить env-файлы с правами 600, но для первого запуска в копиях
   очистить `TELEGRAM_BOT_TOKEN`, `MAX_BOT_TOKEN`, `MAX_WEBHOOK_SECRET`,
   выставить `MAX_WEBHOOK_ENABLED=false`, `AI_SUPPORT_ENABLED=false`.
4. Запускать только `db` и `web`; `telegram-bot`, `max-bot` и
   `catalog-web` не запускать. Порт proxy привязать к `127.0.0.1`.
5. Проверить вход оператора, остатки, заявки, вложения, затем `ops_check`.
6. Только после решения владельца вернуть настоящие токены, DNS и
   запустить ботов. Старый сервер к этому моменту должен быть остановлен,
   иначе два экземпляра будут получать одни и те же обновления ботов.

## Что ещё нужно от владельца

- Ввести парольную фразу для проверки расшифровки архива секретов.
- Подключить внешний накопитель для второй физической копии.
- Хранить парольную фразу не только в Apple Passwords (например, на бумаге
  в сейфе), а `recovery-identity.age` не только на одном Mac.
