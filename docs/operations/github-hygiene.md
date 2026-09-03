# Гигиена GitHub и переносимость выпусков

Аудит только на чтение, 2026-09-02. Ничего не удалено, ничего не переписано,
настройки GitHub не менялись. Документ описывает состояние и план; выполнять его
отдельными подтверждёнными шагами.

Ветку `feature/new-machine-portability` и ветку `feature/disaster-recovery-portability`
этот план не трогает: их ведёт другой агент.

## Что сейчас

| | |
|---|---|
| Каноничный remote | `https://github.com/nikitamaximov2007/DenStock.git` |
| Видимость | **public** |
| Ветка по умолчанию | `main` |
| `main` | `fb601d82570483731b2c65dfec685e9ab9fe033b` |
| Коммитов в `main` | 519, линейная история без merge-коммитов |
| Веток на remote | 98 (включая `main`) |
| Тегов | 1: `v1.0-local`, аннотированный, коммит `6fb2a75`, 2026-06-30 |

История `main` линейна: выпуски приходили только fast-forward. Это делает теги
надёжными указателями на выпуск.

## Достижимость производственных версий

Все известные production-версии достижимы из `main`, то есть откат по коду
возможен без поиска потерянных веток.

| Версия | Дата | Достижима из `main` |
|---|---|---|
| `fb601d82570483731b2c65dfec685e9ab9fe033b` | 2026-09-02 | да |
| `41a312c775f8b9f43273188c5ffd194356f13a95` | 2026-09-01 | да |
| `15c0ed0c7a5688a42ef664d08ea728320e13fc7b` | 2026-08-31 | да |
| `730b5bba06f123156b73783cc1c9c3faa534f387` | 2026-08-30 | да |
| `fe4bbc938a985efc8002cec27cf40e4a636d1544` | 2026-08-28 | да |

## Классификация веток

Проверялось командой, а не по имени ветки: `merge-base --is-ancestor` для
слияния и `git cherry` для сравнения по содержимому патча.

- **Предок `main`: 82.** Удаление безопасно, весь код остаётся в `main`.
- **Не предок, но каждый патч уже в `main` (перенесены cherry-pick): 6.**
  Удаление безопасно.
- **Не слиты, есть собственные патчи: 9.** Удалять нельзя без решения.

### Держать: активная работа

- `main`
- `feature/new-machine-portability` - другой агент, ветка движется
- `feature/disaster-recovery-portability` - другой агент

### Держать: не слитая работа, требует решения

- `integration/canonical-backup-retention` - четыре файла, которых в `main` нет:
  `docs/operations/canonical-backup-retention.md`,
  `scripts/operations/denstock-backup-capped`,
  `scripts/operations/install-denstock-backup-capped.sh`,
  `tests/test_canonical_backup_retention.py`. Работа не выпускалась.
- `feature/wst-knowledge-collector` - 31 файл, которых в `main` нет
  (исследовательский сборщик в `tools/research/`). Ветка старая, отстала на 389
  коммитов, но содержимое уникально.
- `feature/customs-export-completeness` - один патч. Сам запрет неполной выгрузки
  в `main` уже есть, но состав файлов расходится, нужен разбор.
- `release/customer-workflow-nightly-rc` и `release/operator-actions-simplification-rc` -
  патчи уникальны по patch-id, но обе миграции
  (`customers/0002_customerperiodpaymentacknowledgement` и
  `writeoffs/0003_writeoffdocument_business_author`) уже лежат в `main`
  побайтово теми же файлами. Миграции не потеряются.
- `feature/quick-writeoff-location` и `release/writeoff-location-selection-rc` -
  указывают на один и тот же коммит `b926520`, а их
  `templates/writeoffs/write_off_quick.html` побайтово совпадает с `main`:
  работа доехала другим путём.

### Кандидаты на удаление, партия 1 - слиты, до 2026-08-01, 22 шт.

- `codex/add-polaris-catalog`
- `codex/fix-ai-launcher-cap-setuid`
- `feature/batch-scanner-receiving`
- `feature/counting-list-live-location-sort`
- `feature/counting-scanner-focus`
- `feature/customs-application-editor`
- `feature/denis-research-collector`
- `feature/parts-list-article-column`
- `feature/repair-return-source-restoration`
- `feature/storage-location-code-rename`
- `feature/storage-location-rename-hardening`
- `feature/unified-price-settings`
- `feature/unified-whole-number-display`
- `feature/unify-part-identity-ui`
- `feature/warehouse-financial-statistics`
- `fix/customs-excel-export`
- `fix/customs-export-formatting`
- `fix/return-complete-hotfix`
- `fix/return-postgres-row-lock`
- `fix/storage-location-barcode-reuse`
- `fix/warehouse-action-identity-cancel`
- `fix/warehouse-action-snapshot-repair`

### Кандидаты на удаление, партия 2 - слиты, 2026-08-01 .. 2026-08-26, 29 шт.

- `codex/ai-support-quality-context`
- `feature/customs-historical-source-of-truth`
- `feature/emergency-install-ready`
- `feature/operator-reports-and-manual-part`
- `feature/reports-brp-ui-cleanup`
- `feature/sidebar-all-parts`
- `fix/ai-support-knowledge-image`
- `fix/ai-support-receiving-vocabulary`
- `integration/customs-on-repair-plus-analog`
- `integration/repair-no-zero-prices-current-prod`
- `integration/repair-plus-analog-current-prod`
- `integration/warehouse-next-batch`
- `release/ai-socket-boot-order-rc`
- `release/analog-supplier-excel-import-rc`
- `release/brp-use-article-alias-rc`
- `release/client-payment-status-rc`
- `release/customs-historical-source-of-truth-rc`
- `release/emergency-physical-install-rc`
- `release/repair-no-zero-prices-rc`
- `release/repair-plus-analog-rc`
- `release/reports-brp-ui-cleanup-rc`
- `release/search-not-found-article-rc`
- `release/sidebar-all-parts-rc`
- `release/storage-drawer-zero-rc`
- `release/warehouse-analogs-rc`
- `release/warehouse-client-totals-rc`
- `release/warehouse-create-part-rc`
- `release/warehouse-next-batch-rc`
- `release/warehouse-next-rc`

### Кандидаты на удаление, партия 3 - слиты, с 2026-08-27, 31 шт.

Удалять только после того, как появятся теги выпусков: эти ветки соответствуют
недавним выпускам и пока служат единственной понятной подписью к ним.

- `feature/aftermarket-rub-pricing`
- `feature/customs-operator-workflow`
- `feature/customs-provenance`
- `feature/operator-quick-actions-sale-cleanup`
- `feature/operator-report-history-ux`
- `feature/operator-workflow-completion`
- `feature/secondary-return-entry`
- `feature/unified-operator-price`
- `integration/customer-operator-plus-price-current-prod`
- `integration/customers-cancellation-current-prod`
- `integration/historical-customers-backfill-qualification`
- `integration/nightly-production-cumulative`
- `integration/report-sale-repair-cancellation-current`
- `release/aftermarket-catalog-source-status-rc`
- `release/aftermarket-rub-pricing-rc`
- `release/customer-operator-current-prod-rc`
- `release/customer-operator-plus-price-current-prod-rc`
- `release/customers-cancellation-current-prod-rc`
- `release/customs-provenance-completeness-rc`
- `release/live-inventory-cell-state-rc`
- `release/nightly-functional-recovery-rc`
- `release/nightly-production-cumulative-rc`
- `release/observability-price-ux-rc`
- `release/old-price-label-on-sidebar`
- `release/operator-report-history-ux-current`
- `release/operator-workflow-completion-rc`
- `release/partial-repair-line-cancellation-current`
- `release/sidebar-scanner-cleanup-on-customs`
- `release/storage-code-reuse-rc`
- `release/unified-operator-price-rc`
- `release/zero-price-sale-guard-rc`

### Кандидаты на удаление, партия 4 - перенесены cherry-pick, 6 шт.

- `feature/old-price-label`
- `feature/repair-no-zero-prices`
- `feature/sidebar-scanner-cleanup`
- `feature/storage-code-reuse`
- `hotfix/emergency-shell-lf`
- `release/partial-repair-line-cancellation-rc`

## Теги выпусков

Сейчас тег один и к производственным выпускам отношения не имеет.

**Соглашение:** `prod-YYYYMMDD-HHMM-<short sha>`, время MSK, тег аннотированный.
Пример: `prod-20260901-2328-fb601d8`.

Имя сортируется по времени, читается человеком, и версия видна прямо в имени,
поэтому по тегу можно сразу сделать `git checkout` для отката, не заглядывая
внутрь.

**Когда ставится тег.** Только после того, как выпуск полностью состоялся:

1. квалификация пройдена;
2. подписанная резервная копия PRE проверена и ушла offsite;
3. выкладка выполнена;
4. дым-проверка на production прошла;
5. подписанная резервная копия POST проверена и ушла offsite;
6. `main`, `origin/main`, production HEAD и работающая версия совпали.

Тег ставится на ту же версию, что стоит в `DENSTOCK_APP_COMMIT`, и после этого не
двигается. Переносить тег запрещено: если выпуск оказался неудачным, ставится
новый тег на новую версию, а старый остаётся следом истории.

В теле аннотированного тега держать три строки: что выпущено, номера копий PRE и
POST, количество миграций до и после.

**Базовые теги, которые стоит проставить задним числом.** Версии проверены на
production и достижимы из `main`.

| Тег | Версия | Что это |
|---|---|---|
| `prod-20260831-1904-15c0ed0` | `15c0ed0c7a5688a42ef664d08ea728320e13fc7b` | Customs operator workflow, миграция `actions.0011`, 109 -> 110 |
| `prod-20260901-2227-41a312c` | `41a312c775f8b9f43273188c5ffd194356f13a95` | Sidebar scanner cleanup, без миграций, 110 |
| `prod-20260901-2328-fb601d8` | `fb601d82570483731b2c65dfec685e9ab9fe033b` | Old price label, без миграций, 110 |

Более ранние версии (`730b5bb`, `fe4bbc9`) достижимы из `main`, но точное время
их выкладки в репозитории не записано. Подписывать их временем задним числом не
нужно: либо не тегировать вовсе, либо тегировать без времени.

## Видимость и доступ

Репозиторий публичный. Секретов в нём не найдено, поэтому срочности нет, но
частный репозиторий уместнее: это учётная система реального склада, публичной
пользы от неё нет.

Что проверить до переключения в private:

1. **Выкладка.** Production клонирует по HTTPS. После перевода в private нужен
   способ аутентификации на сервере: deploy key только на чтение (предпочтительно)
   либо fine-grained token. На сервере лежит рабочий клон, и `git fetch` на нём
   обязан продолжать работать.
2. **Восстановление на чистой машине.** У того, кто восстанавливает, должен быть
   доступ к учётной записи или свой deploy key. Приватность не должна закрыть путь
   восстановления именно тогда, когда он понадобится.
3. **Агенты.** Их доступ идёт через учётные данные владельца; проверить один раз
   после смены видимости, а не в момент аварии.
4. **Внешних интеграций и workflow нет**, поэтому публичным клонированием сейчас
   никто не пользуется.

Учётная запись: включить двухфакторную аутентификацию, сохранить коды
восстановления вне ноутбука, проверить резервную почту. Passkey уместен как
второй способ входа, но коды восстановления важнее: они спасают, когда телефона
нет под рукой.

## Секреты

Проверены имена всех 1091 путей, когда-либо попадавших в достижимую историю, и
содержимое всех 98 вершин веток.

- Ни `.env`, ни `rclone.conf`, ни `auth.json`, ни `*.pem`, ни `id_rsa`, ни
  `id_ed25519`, ни `*.key` в историю не попадали ни разу.
- В `main` лежат только шаблоны: `.env.example`, `.env.backup.example`,
  `.env.emergency.example`, `tools/research/.env.research.example`.
- Совпадения по словам «PRIVATE KEY» и по формату ключей Yandex найдены только в
  коде редактирования диагностики и в тестах, где значения явно поддельные.
- Пароль базы из рабочего `.env` в истории не встречается.

Переписывать историю не нужно, ротация ключей по итогам аудита не требуется.

## Защита `main`

Правила должны защищать от случайной потери истории и не мешать нынешнему
порядку, где выкладку ведёт один процесс через fast-forward.

Включить:

- запрет `force push` в `main`;
- запрет удаления `main`;
- запрет удаления и перезаписи тегов, иначе подпись выпуска перестаёт быть
  доказательством.

Не включать сейчас:

- обязательный pull request и обязательное ревью: решение принимает один
  человек, а выпуск идёт fast-forward из ветки выпуска. Обязательный PR добавит
  шаг, но не добавит проверяющего;
- обязательные проверки статуса: CI в репозитории нет, требование зелёного
  статуса заблокирует выкладку насовсем.

Появится CI - тогда и включать обязательные проверки, а не заранее.

## Переносимость на новый ноутбук

Из GitHub восстанавливается всё несекретное: код, миграции, зафиксированные
версии зависимостей (`requirements/production.txt`, `requirements/dev.txt`),
`docker-compose.yml`, `docker/Dockerfile`, `docker/entrypoint.sh`, шаблоны
переменных окружения, руководства в `docs/operations/`, тесты.

Пробелы:

1. **`docker-compose.signing.yml` никогда не был в Git.** На production он лежит
   неотслеживаемым файлом и подключается через `COMPOSE_FILE`. Секретов в нём
   нет, только монтирование каталога с ключом подписи только на чтение. Если
   сервер потерян, файл придётся восстанавливать по памяти. Его следует
   закоммитить, оставив секретом сам ключ.
2. **Нет записи о том, что и когда выкладывалось.** Это закрывают теги выпусков.
3. **Ночные заметки о состоянии выпуска не отслеживаются Git.** Это осознанно,
   но значит, что новый ноутбук их не увидит: всё, что должно пережить машину,
   обязано попадать в `docs/`.

Секреты остаются снаружи: `.env` на сервере, конфигурация rclone, приватный ключ
подписи в `/etc/denstock/manifest-signing`.

## Порядок выполнения

Каждый шаг отдельно и с подтверждением. Ничего из этого сегодня не выполнено.

**Шаг 1. Учётная запись и видимость.** Двухфакторная аутентификация, коды
восстановления, резервная почта. Затем deploy key только на чтение для
production и проверка `git fetch` на сервере. Только после этого перевод
репозитория в private.

**Шаг 2. Теги выпусков.** Проставить три базовых тега задним числом, затем
включить тег в порядок выпуска обязательным последним шагом.

**Шаг 3. Защита.** Запрет force push и удаления для `main`, запрет перезаписи
тегов.

**Шаг 4. Удаление веток партиями.** Партия 1, проверка, партия 2, проверка,
партия 4. Партия 3 только после шага 2. Между партиями делать
`git fetch --prune` и убеждаться, что `main` не сдвинулся.

**Шаг 5. Проверка с нуля.** Клонировать репозиторий в пустой каталог на другой
машине, поднять окружение по README и убедиться, что тесты идут, а по тегам
видно, какая версия сейчас на production.

## Что этот аудит не делал

Ветки не удалялись, теги не создавались и не удалялись, история не
переписывалась, видимость и защита не менялись, production не трогался.
