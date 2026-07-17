# ИИ-поддержка: архитектура и эксплуатация

## Граница безопасности

`apps.ai_support` является отдельным read-only приложением. HTTP views вызывают
только собственный service layer, provider interface, локальный лексический
retrieval и сборщик безопасной диагностики. Генерация выполняется отдельным
процессом Codex CLI с авторизацией через подписку ChatGPT. OpenAI Python SDK и
usage-based API в этой схеме не используются. Отдельный API key не требуется,
отдельная оплата API usage не выполняется. При этом действуют лимиты Codex,
включённые в конкретную подписку ChatGPT, и они не являются безграничными.

У provider нет разрешённых tools, command execution, SQL, web search, MCP,
plugins, apps, hooks или доступа к изменяющим сервисам DenisStock. Ответ
сохраняется и выводится как обычный escaped text. Если JSONL содержит tool event,
процесс останавливается, а пользователю показывается безопасная ошибка.

Prompt injection нельзя исключить полностью. Основные рубежи: отдельный
системный пользователь, изолированные `CODEX_HOME` и runtime, отсутствие
репозитория и production-данных в runtime, read-only sandbox, минимальный
environment, timeout, лимиты вывода и завершение всей process group.

Никогда не передаются cookies, session/CSRF/Authorization tokens, произвольные
headers, query string, environment, SQL, логи, дампы БД, email пользователя,
system prompt как сохранённые данные или полный event stream Codex. Prompt,
stderr, auth output и изображения не пишутся в application log.

## Авторизация ChatGPT

Авторизацию выполняет администратор отдельно от HTTP-запроса:

```bash
sudo -u denstock-ai env CODEX_HOME=/var/lib/denstock-ai/codex-home codex login
sudo -u denstock-ai env CODEX_HOME=/var/lib/denstock-ai/codex-home codex login status
```

Для headless-сервера:

```bash
sudo -u denstock-ai env CODEX_HOME=/var/lib/denstock-ai/codex-home codex login --device-auth
```

Provider перед каждым запуском выполняет только неинтерактивный
`codex login status`. Он не запускает login из web request и принимает только
статус авторизации через ChatGPT. При отсутствии авторизации пользователь видит
сообщение о временно не настроенной поддержке и может создать ручное обращение.

Файлы credentials находятся только в отдельном `CODEX_HOME`. В частности,
`auth.json` нельзя копировать в репозиторий, `.env`, Django DB, application log,
обычный backup DenisStock или пользовательский ответ. Каталог должен принадлежать
`denstock-ai` и иметь минимальные filesystem permissions.

## Изолированный runtime

До production-включения создайте отдельного Linux user `denstock-ai`. Он не
должен иметь доступ к `/opt/denstock`, Docker socket, PostgreSQL, SSH, systemd,
backup, media, deployment directories и домашнему каталогу основного
пользователя. Настройка пользователя, sudoers и production launcher не входит в
данную feature-ветку.

`AI_SUPPORT_CODEX_BINARY` в production может указывать на root-owned launcher,
который передаёт только аргументы Codex и запускает CLI от `denstock-ai`. Не
разрешайте произвольную командную строку. Web user может иметь доступ только к
изолированному runtime для создания request-каталога; `CODEX_HOME` с credentials
ему читать нельзя.

Runtime не должен пересекаться с `BASE_DIR`, public/private media, backup или
`CODEX_HOME`. Django security check отклоняет такие конфигурации. В request-каталог
попадают только schema, нормализованная временная копия изображения и временные
файлы одного запуска. Каталог удаляется после success, timeout, non-zero exit и
ошибки parsing.

## Настройки

Функция по умолчанию выключена:

```text
AI_SUPPORT_ENABLED=false
AI_SUPPORT_PROVIDER=disabled
AI_SUPPORT_CODEX_BINARY=codex
AI_SUPPORT_CODEX_MODEL=
AI_SUPPORT_CODEX_HOME=/var/lib/denstock-ai/codex-home
AI_SUPPORT_CODEX_WORKSPACE=/var/lib/denstock-ai/runtime
AI_SUPPORT_CODEX_TIMEOUT_SECONDS=60
AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES=65536
AI_SUPPORT_CODEX_MAX_STDERR_BYTES=16384
AI_SUPPORT_CODEX_MAX_PROMPT_CHARS=24000
AI_SUPPORT_CODEX_MAX_HISTORY_CHARS=12000
AI_SUPPORT_CODEX_MAX_CONCURRENT=2
```

После подготовки изоляции задайте `AI_SUPPORT_PROVIDER=codex_cli` и включите
feature flag. Модель намеренно не зашита: пустой `AI_SUPPORT_CODEX_MODEL`
считается configuration error. Fake provider разрешается только тестовыми
settings.

Глобальный лимит процессов сериализуется DB-backed singleton gate и активными
usage rows. Дополнительный process-local semaphore защищает каждый Django worker.
Один пользователь не может иметь два активных запроса.

## Команда Codex CLI

Для текущей проверенной версии CLI provider строит массив аргументов, а prompt
передаёт через stdin. `--image` добавляется только для backend-пути временной
нормализованной копии:

```text
codex exec
  --ephemeral
  --sandbox read-only
  -c approval_policy="never"
  -c web_search="disabled"
  -c mcp_servers={}
  --strict-config
  --skip-git-repo-check
  --ignore-user-config
  --ignore-rules
  --json
  --output-schema <request-dir>/support-response.schema.json
  --model <AI_SUPPORT_CODEX_MODEL>
  --cd <request-dir>
  [--image <request-dir>/attachment.<ext>]
  -
```

У установленной версии нет отдельного флага `--ask-for-approval`, поэтому
запрет задаётся strict config override `approval_policy="never"`. Запуск всегда
использует `shell=False`, фиксированный cwd, минимальный environment и новый
process group. `--output-schema` допускает только непустое строковое поле
`answer` ограниченной длины. Reasoning и progress игнорируются, hidden reasoning
и event stream не сохраняются.

## Knowledge, история и изображения

Production retrieval читает только allowlisted Markdown из
`apps/ai_support/knowledge_pack/`. Он детерминированный, без сети, выбирает не
более четырёх фрагментов общим размером до 6000 символов. Репозиторий, тесты,
старые design docs и operational secrets не индексируются.

Каждый запрос является `--ephemeral`. Ограниченная история формируется из Django
DB, а `codex exec resume` не используется. Разрешено одно JPG, PNG или WEBP
изображение до 5 МБ и 20 мегапикселей. Pillow полностью декодирует файл,
запрещает анимацию, удаляет метаданные повторным кодированием и сохраняет файл
под UUID. Исходное имя не сохраняется.

`PRIVATE_MEDIA_ROOT` не должен совпадать с `MEDIA_ROOT`. Caddy не получает доступ
к private media. Файл выдаёт authenticated Django view с ownership или manager
ticket capability, `Cache-Control: private, no-store` и `nosniff`.

## Права, квоты и деградация

`use_ai_support` получают все рабочие роли. `manage_ai_support_tickets` получают
Администратор и Руководитель. Каждый view проверяет capability на сервере.
Обычный пользователь видит только свои разговоры и вложения. Manager получает
только явно выбранный snapshot обращения.

DB-backed quota ограничивает запросы в минуту и сутки, числовой token usage,
один активный запрос пользователя и общий уровень параллельности. Django не
знает остаток подписки. Usage limit Codex нормализуется как
`subscription_quota_exceeded`; stderr и credential details не показываются.
При timeout, ошибке авторизации, исчерпании лимита или выключенном feature flag
ручное обращение остаётся доступным.

## Retention

Значения по умолчанию: вложения 30 дней, разговоры и обращения 180 дней.
Команда всегда начинает с dry-run:

```bash
python manage.py purge_ai_support_data
python manage.py purge_ai_support_data --confirm
```

Текущий offsite backup pipeline не изменён и private screenshots автоматически
в него не включены. `CODEX_HOME` и credentials также не должны включаться в
backup DenisStock.

## Обновление Codex CLI

1. Оставьте `AI_SUPPORT_ENABLED=false` или `AI_SUPPORT_PROVIDER=disabled`.
2. Установите новую версию CLI штатным package manager вне репозитория.
3. Проверьте `codex --version`, `codex exec --help` и наличие используемых флагов.
4. От `denstock-ai` выполните `codex login status`, не публикуя вывод credentials.
5. Запустите unit tests с fake subprocess и Django checks.
6. Выполните отдельную staging-проверку без production data.
7. Только после ручного решения включите feature flag.

Если флаги или JSONL contract изменились, provider сначала обновляется и
проходит полный test suite. Автоматический fallback на API или другой cloud
provider запрещён.

## Диагностика

Route принимается только как path без query/fragment, проверяется через Django
`resolve` и allowlist route names. Browser family и viewport нормализуются. App
commit читается из `DENSTOCK_APP_COMMIT`; HTTP request не запускает git. В логах
допустимы только UUID, user id, provider/model, status, latency, числовой usage,
безопасный request id и safe error code.

При недоступности проверьте feature flag, provider, обязательную модель,
существование и изоляцию runtime-каталогов, права launcher и результат
`codex login status`. Не запускайте реальный provider из pytest.
