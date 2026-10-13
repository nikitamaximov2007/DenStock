# VPS И YANDEX ПОТЕРЯНЫ ОДНОВРЕМЕННО: КАК ВОССТАНОВИТЬ DENISSTOCK

Документ для худшего случая: сервер 185.250.44.206 недоступен навсегда, бакет
Yandex Object Storage тоже потерян (удалён, заблокирован, аккаунт недоступен).
Остаются только независимые копии: зашифрованный комплект восстановления на
Mac владельца, внешний диск (если сделан) и независимый провайдер (если
подключён). Секретов в этом документе нет: только имена файлов и команды.

Цели: RPO не больше 24 часов, RTO не больше 8 часов. Это инженерные цели, их
подтверждают замеры (раздел 20), а не этот текст.

## 1. Когда применять этот документ

- Сервер не отвечает по SSH и в панели хостинга, восстановить его нельзя.
- В Yandex Object Storage нет пригодных копий или нет доступа к аккаунту.
- Если жив хотя бы один из двух, используйте обычный
  [restore-runbook.md](restore-runbook.md) и
  [scheduled-offsite-backups.md](scheduled-offsite-backups.md): они быстрее.

Какие сценарии покрыты и что не доказано: [disaster-recovery-failure-matrix.md](disaster-recovery-failure-matrix.md).

Перед началом: старый сервер, если он хоть как-то жив, должен быть ВЫКЛЮЧЕН
(раздел 13). Два живых экземпляра с одним токеном Telegram или MAX делят
сообщения клиентов между собой.

## 2. Что нужно для восстановления

| Что | Где лежит | Без этого |
| --- | --- | --- |
| Парольная фраза ключа восстановления | Apple Passwords, запись "DenisStock - Emergency Recovery", и бумажная копия (раздел 5) | Ничего не расшифровать. Это единственная невосстановимая часть |
| `keys/recovery-identity.age` | `~/DenisStock-Recovery/keys/` и вторая копия ключа | То же |
| Подписанная копия `*.signed-backup.tar.age` | `~/DenisStock-Recovery/data/`, `~/DenisStock-Recovery-Updates/<дата>/`, независимый провайдер | Нет базы и media |
| `private_media.*.tar.age` и `.inventory.age` | там же (для копий до релиза backup-only) | Нет вложений заявок и прайсов. В новых копиях private_media внутри подписанного архива |
| `production-secrets-*.tar.age` | `~/DenisStock-Recovery/secrets/` | Нет `.env`, токенов ботов, ключа подписи |
| `source-code.<commit>.bundle.age` | комплект | Можно взять из GitHub, если доступен |
| Публичный ключ подписи `production-manifest-ed25519-public.pem` | `~/Desktop/DenisStock-Emergency-Transfer/` и внутри секретов | Подпись проверить нечем |

Какая копия свежее: сравните дату в имени `*.signed-backup.tar.age`. Берите
самую новую подписанную копию, если она проходит проверку (раздел 7).

## 3. Новый сервер

- Ubuntu 24.04 LTS, 2 vCPU, 4 ГБ RAM, 40 ГБ диска, публичный IPv4.
- Docker Engine 29.x и Docker Compose 5.x из официального репозитория Docker.
- `age`, `git`, `python3-cryptography`, `rclone`, `lsof`: `apt install age git python3-cryptography rclone lsof`.
- PostgreSQL ставить НЕ нужно: он работает в контейнере `postgres:16`.
- Каталог `/opt/denstock` создаётся заново, владелец root, права 0750.

Расшифровку и проверочный запуск можно (и лучше) сделать сначала на Mac
владельца: на сервер затем переносятся уже проверенные файлы.

## 4. Где взять архивы

1. Mac владельца: `~/DenisStock-Recovery` (исходный комплект, не изменять) и
   `~/DenisStock-Recovery-Updates/<дата>/` (более свежие захваты).
2. Внешний диск, если копия сделана (раздел 5).
3. Независимый провайдер (раздел 6): объект `<RUN>.signed-backup.tar.age` и
   рядом `<RUN>.signed-backup.tar.age.json` (подтверждение с хешем).

4. GitHub Releases (если канал включен,
   [github-release-backups.md](github-release-backups.md), раздел 9):
   `github_release_backup.py fetch` скачивает новейшую целую копию и сверяет
   SHA-256; секреты при этом создаются заново (`--fresh-secrets`).

Никогда не работайте с единственным экземпляром: сначала скопируйте архив в
новую папку, исходник не трогайте.

## 5. Ключ и парольная фраза

- Без парольной фразы восстановление невозможно. Нужны минимум два способа ее
  получить: Apple Passwords и бумажная запись в запечатанном конверте в
  отдельном физическом месте (не рядом с Mac).
- Файл `recovery-identity.age` сам зашифрован этой фразой, поэтому его вторую
  копию можно хранить рядом с архивами (внешний диск, независимый провайдер).
- Фразу вводит только владелец, в терминале, на запрос `age`. Ее не вставляют
  в чат, в аргументы команд и в файлы.

## 6. Независимый провайдер

Если ежедневная независимая выгрузка включена
(`docs/operations/independent-offsite.md` в ветке `claude/independent-offsite-recovery`), скачайте нужную копию
ключами только для чтения:

```bash
rclone --config <файл> lsf <remote>:<bucket>/denstock
rclone --config <файл> copyto <remote>:<bucket>/denstock/<RUN>.signed-backup.tar.age ./kit/<RUN>.signed-backup.tar.age
rclone --config <файл> copyto <remote>:<bucket>/denstock/<RUN>.signed-backup.tar.age.json ./kit/<RUN>.signed-backup.tar.age.json
shasum -a 256 ./kit/<RUN>.signed-backup.tar.age
```

Хеш обязан совпасть с `ciphertext_sha256` в `.json`. Если не совпал, берите
предыдущую копию: эту не расшифровывать.

## 7. Проверка до расшифровки

```bash
shasum -a 256 -c <файл>.sha256        # для архивов, рядом с которыми лежит .sha256
age --version                          # age должен быть установлен
```

Подпись Ed25519 и SHA-256 каждого файла копии проверяет
`scripts/dr/offline_restore.sh` на шаге 1, до любых изменений. Подпись
проверяется ключом, закреплённым заранее, а не ключом из архива.

## 8. Расшифровка: один скрипт

```bash
scripts/dr/prepare_kit_restore.sh --kit <папка с *.age> \
  --identity ~/DenisStock-Recovery/keys/recovery-identity.age \
  --work <НОВАЯ папка> --secrets ~/DenisStock-Recovery/secrets/production-secrets-<дата>.tar.age
```

- Скрипт спрашивает фразу, сначала проверяет, что ключ подходит, и только
  потом пишет файлы. Папка `--work` создаётся с правами 0700 и должна не
  существовать.
- Внутри: `run/` (база, media, manifest и, в новых копиях,
  `private_media.tar.gz`), `private.tar` и `private-inventory.json` (только для
  старых копий), `secrets/`, `source/` на коммите из manifest, `env/`,
  `pinned-public.pem`.
- В конце скрипт печатает точную команду следующего шага.

## 9. База данных

`offline_restore.sh` делает это сам, порядок важен:

1. Новый пустой PostgreSQL 16.
2. Роль `denstock_public` создаётся ДО восстановления: в дампе есть права на
   нее. Пароль берётся из `PUBLIC_DATABASE_URL` в `.env.public` и передаётся
   через stdin.
3. `pg_restore --no-owner --exit-on-error`.
4. `scripts/operations/create_public_catalog_role.sql`: настройки роли
   (только чтение, таймауты) живут на уровне кластера и в дамп не попадают.

## 10. Публичные media

`media.tar.gz` распаковывается в новый том `/app/mediafiles` от имени
пользователя образа (UID 2001). Проверка: дерево файлов совпадает с
`media_tree_sha256` из подписанного manifest.

## 11. Приватные файлы (private_media)

- Новые копии: `private_media.tar.gz` внутри подписанной копии. Хеш архива
  подписан, дерево сверяется с `private_media_tree_sha256`.
- Старые копии: отдельный `private_media.*.tar.age`. Архив снят с тома и имеет
  верхний каталог `_data/`: скрипт снимает его сам (`--strip-components=1`).
  Каждый файл сверяется с независимой описью `inventory`.
- Приватные файлы никогда не попадают в публичный каталог `/media/`: прокси
  отвечает 404 на `/media/customer_requests/` и `/media/private_attachments/`.

## 12. Первый запуск: изолированный

```bash
scripts/dr/offline_restore.sh --project dr-test-<дата> --source <work>/source --run <work>/run \
  --env-dir <work>/env --pubkey <work>/pinned-public.pem --port 18080 --mode test \
  --report <work>/report.json
```

Для старых копий добавить `--private-archive <work>/private.tar --private-inventory <work>/private-inventory.json`.

Что гарантирует `scripts/dr/compose.dr.yml`:

- сеть приложения `internal`: наружу нет выхода (скрипт проверяет, что
  соединение с 1.1.1.1:443 блокируется);
- наружу открыт только прокси на 127.0.0.1;
- токены Telegram и MAX пустые, webhook MAX выключен, AI выключен, ботов и
  таймеров в стеке нет, ключ подписи не подключён, восстановление из веба
  выключено;
- миграции при старте обязаны быть пустыми ("No migrations to apply").

Скрипт сам проверяет данные (раздел 18). Отчёт: `report.json`, поле
`passed` должно быть `true`. После проверки: `scripts/dr/cleanup_dr.sh
dr-test-<дата>`.

## 13. Старый сервер и боты: без двойных получателей

- Telegram: один токен может читать только один получатель. Перед запуском
  нового `telegram-bot` убедитесь, что старый сервер выключен в панели
  хостинга. Если сервер у хостинга "завис" и может ожить, перевыпустите токен у
  @BotFather (`/revoke`) и запишите новый в `.env.telegram`.
- MAX: webhook один на бота. Новый адрес ставится при старте `max-bot` из
  `MAX_PUBLIC_WEBHOOK_URL`; старый адрес перестаёт работать вместе с DNS.
- Включать ботов только после раздела 15 (DNS и сертификат), иначе MAX не
  доставит обновления на новый адрес.

## 14. Настройки, которые зависят от IP и домена

Перенесите `.env*` из `secrets/opt/denstock/` в `/opt/denstock/` (права 0600)
и проверьте значения:

| Переменная | Файл | Что проверить |
| --- | --- | --- |
| `CADDY_SITE_ADDRESS` | `.env` | домен или новый IP |
| `DJANGO_ALLOWED_HOSTS` | `.env` | новый IP и домены |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `.env` | `https://` + домены |
| `DENSTOCK_PUBLIC_BASE_URL` | `.env` | адрес публичного каталога |
| `TELEGRAM_INTERNAL_BASE_URL` | `.env.telegram` | внутренний адрес web |
| `MAX_PUBLIC_WEBHOOK_URL` | `.env.max-webhook` | `https://<домен>/customer-requests/max/webhook/` |
| `DJANGO_PUBLIC_ALLOWED_HOSTS` | `.env.public` | домен каталога |

Также из секретов: `/etc/denstock/caddy/Caddyfile` (живой Caddyfile с
маршрутом MAX и защитой приватных файлов), `/etc/denstock/max/` (корневой
сертификат для API MAX), `/etc/denstock/manifest-signing/` (ключ подписи, права
0600, владелец UID 2001), `/opt/denstock/docker-compose.signing.yml`,
`/etc/denstock-ai/` (только если нужен AI-помощник). `COMPOSE_FILE` в
`.env` указывает, какие compose-файлы подключать.

## 15. DNS и сертификат

1. У регистратора `pro-brp.ru` поменять A-записи (корень, `www`, домен
   каталога) на новый IP. TTL заранее держать небольшим (300 секунд).
2. Дождаться: `dig +short pro-brp.ru` показывает новый IP.
3. Caddy сам выпускает сертификаты Let's Encrypt при первом запросе (порты 80
   и 443 открыты). Проверка: `curl -sI https://pro-brp.ru/healthz`.
4. Google Search Console и Яндекс Вебмастер трогать не нужно: подтверждение
   по домену сохраняется.

## 16. Включение рабочего сервиса

```bash
cd /opt/denstock && git clone <bundle или GitHub> . && git checkout <app_commit из manifest>
docker compose up -d db
# роль, pg_restore, create_public_catalog_role.sql, media, private_media: как в offline_restore.sh шаги 3-5
docker compose up -d web catalog-web proxy
docker compose logs web | grep "No migrations to apply"
docker compose up -d telegram-bot max-bot      # только после разделов 13 и 15
```

Удобнее всего повторить шаги 3-5 `offline_restore.sh` вручную на сервере или
восстановить с ним в проект `dr-*`, проверить и затем поднять рабочий стек на
тех же файлах.

## 17. Первая новая резервная копия

Сразу после запуска: `docker compose exec web python manage.py backup_all
--trigger manual`, затем проверка в разделе "Резервные копии" админки. Новый
бакет Yandex (или другой S3) и ключи настраиваются по
[scheduled-offsite-backups.md](scheduled-offsite-backups.md); старые ключи
Yandex считать скомпрометированными. Независимую выгрузку включить по
`docs/operations/independent-offsite.md` (ветка `claude/independent-offsite-recovery`).

## 18. Как убедиться, что восстановление полное

`validate_restore.py` проверяет и печатает `DR-VALIDATION`:

- коммит приложения равен коммиту копии; миграции равны, неизвестных нет;
- отпечаток бизнес-данных равен подписанному;
- все таблицы моделей на месте; нет "висячих" внешних ключей;
- остатки (`check_stock_balance`) сходятся с первичными документами;
- публичные и приватные файлы совпадают с подписанными хешами или описью;
- все ссылки на файлы открываются.

HTTP-проверки: `/healthz`, вход, каталог, поиск, склад, клиенты, движения,
остатки; приватные файлы через `/media/` дают 404.

## 19. Что потеряно и что сказать людям

- Потеряно всё после времени создания копии (`created_at` в manifest): новые
  продажи, приёмки, заявки и сообщения ботов за этот промежуток.
- Сверить последние продажи и приёмки по бумажным документам и перепровести
  через обычные формы (никогда не править остатки напрямую).
- Клиенты ботов: сообщения, отправленные во время простоя, Telegram хранит до
  24 часов; MAX повторяет доставку ограниченное время.

## 20. Замеры RTO

| Прогон | Данные | Время |
| --- | --- | --- |
| Синтетический, из папок | синтетика | 21 с (restore), 59 с весь прогон |
| Синтетический, через зашифрованный комплект | синтетика | 21-29 с restore, около 60 с весь прогон |
| Реальные данные, полный сервис | 34 МБ дамп, 12 МБ media, 51 МБ private | НЕ ИЗМЕРЕНО: нужен ввод фразы владельцем, см. `scripts/dr/morning_real_drill.sh` |

Оценка полного RTO на новом сервере: заказ VPS и установка Docker 30-60 мин,
перенос и расшифровка 10-20 мин, восстановление и проверка 5-10 мин, DNS и
сертификат 15-60 мин. Итого 1-3 часа при доступной фразе и комплекте. Это
оценка, а не замер.
