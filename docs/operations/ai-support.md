# ИИ-поддержка: архитектура и эксплуатация

## Статус production

ИИ-поддержка по умолчанию выключена. Production поддерживает только внешний
Linux launcher из `deploy/ai-support`: Django работает без root, общается с ним
по Unix socket и не запускает Codex напрямую. Включение при `DEBUG=false`
разрешено system check только для `codex_cli + external`, точного handshake,
работающего MAXINIK route и активного ChatGPT login.

Windows поддерживается только для разработки и тестов. Production runtime для
этой интеграции должен быть Linux.

## Граница безопасности

`apps.ai_support` является read-only приложением. Генерация выполняется Codex
CLI с авторизацией через подписку ChatGPT. OpenAI Python SDK, API key и
usage-based API в этой схеме не используются. Действуют лимиты Codex в подписке,
они не являются безграничными.

Provider не получает mutation services DenisStock, SQL, repository, `.env`, DB,
Docker socket, SSH, backup, media или server logs. Prompt передаётся через stdin,
изображение передаётся только как нормализованная временная копия. В application
log не попадают prompt, stderr, auth output, event stream или изображение.

Защита строится несколькими рубежами:

- pinned Codex CLI version;
- exact ChatGPT auth status;
- fail-closed production gate по launcher handshake и ChatGPT auth status;
- отдельные `CODEX_HOME` и runtime;
- `read-only` sandbox и `approval_policy="never"`;
- отключённые tools, apps, plugins, MCP, browser и web search;
- bounded stdin, stdout, stderr и JSONL line;
- общий deadline и завершение process tree;
- строгие JSON Schema и JSONL parser;
- DB-backed global concurrency gate.

## Требуемая версия

Проверенная и обязательная версия:

```text
codex-cli 0.142.5
```

Настройка:

```text
AI_SUPPORT_CODEX_REQUIRED_VERSION=0.142.5
```

Источник истины находится в коде: `AUDITED_CODEX_CLI_VERSION = "0.142.5"`.
Setting является только декларативным подтверждением. Для запуска должны
совпадать code constant, setting и вывод установленного CLI. Изменить audited
version только через environment нельзя.

Перед auth и каждым `exec` provider выполняет `<binary> --version`. При
несовпадении, неожиданном формате, stderr или non-zero exit дальнейшие процессы
не запускаются, результат получает `codex_cli_incompatible`.

Автоматическое обновление CLI запрещено. Любая смена версии требует повторного
compatibility и security audit, проверки config keys, JSONL fixtures, полного
test suite и отдельной staging-приёмки.

## Авторизация ChatGPT

Login выполняется администратором отдельно от HTTP request:

```bash
sudo -u denstock-ai env CODEX_HOME=/var/lib/denstock-ai/codex-home codex login
sudo -u denstock-ai env CODEX_HOME=/var/lib/denstock-ai/codex-home \
  codex login --device-auth
```

Ручная проверка:

```bash
sudo -u denstock-ai env CODEX_HOME=/var/lib/denstock-ai/codex-home \
  codex -c 'forced_login_method="chatgpt"' login status
```

Для 0.142.5 `login status` пишет результат в stderr. Provider принимает только
точный stderr `Logged in using ChatGPT` с одним ожидаемым завершением строки,
пустым stdout и exit code 0. API key или Agent Identity дают
`codex_wrong_auth_method`, signed out даёт `codex_not_authenticated`, любой
изменившийся, дополнительный или смешанный вывод даёт
`codex_auth_status_unknown`. Интерактивный login из Django не запускается.

`auth.json` хранится только в отдельном `CODEX_HOME`. Его запрещено копировать в
git, `.env`, Django DB, application logs, tickets и backup DenisStock.

## Настройки

Безопасные defaults:

```text
AI_SUPPORT_ENABLED=false
AI_SUPPORT_PROVIDER=disabled
AI_SUPPORT_CODEX_BINARY=codex
AI_SUPPORT_CODEX_REQUIRED_VERSION=0.142.5
AI_SUPPORT_CODEX_MODEL=
AI_SUPPORT_CODEX_HOME=/var/lib/denstock-ai/codex-home
AI_SUPPORT_CODEX_WORKSPACE=/var/lib/denstock-ai/requests
AI_SUPPORT_CODEX_LAUNCH_MODE=disabled
AI_SUPPORT_CODEX_LAUNCHER_SOCKET=/run/denstock-ai/launcher.sock
AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION=false
AI_SUPPORT_CODEX_TIMEOUT_SECONDS=60
AI_SUPPORT_CODEX_MAX_OUTPUT_BYTES=65536
AI_SUPPORT_CODEX_MAX_STDERR_BYTES=16384
AI_SUPPORT_CODEX_MAX_PROMPT_CHARS=24000
AI_SUPPORT_CODEX_MAX_HISTORY_CHARS=12000
AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY=1
AI_SUPPORT_CODEX_RUNTIME_RETENTION_HOURS=24
```

Модель не зашита. Пустая модель является configuration error. Отдельной
настройки output tokens нет: CLI 0.142.5 не предоставляет проверенного
version-pinned ограничения для этого provider. Фактические ограничения задают
JSON Schema `maxLength`, stdout cap, prompt cap и timeout.

Для локальной разработки прямой запуск требует одновременно:

```text
DEBUG=true
AI_SUPPORT_CODEX_LAUNCH_MODE=direct_dev
AI_SUPPORT_CODEX_ALLOW_DIRECT_DEV_EXECUTION=true
```

Ни одна из этих настроек не подходит production.
На Windows дополнительно укажите абсолютный путь к native `codex.exe`; npm
`.cmd`, `.ps1` и extensionless shims не принимаются безопасным subprocess.

## Production launcher contract

Режим `external` использует root-owned launcher и framed protocol v1. В web
container монтируются только `/run/denstock-ai/launcher.sock` и
`/var/lib/denstock-ai/requests`; host config, `CODEX_HOME`, repository и proxy
secrets не монтируются. Контракт реализации:

- Django web user не запускает Codex напрямую;
- Codex запускается как отдельный unprivileged user `denstock-ai`;
- binary Codex и набор допустимых аргументов зафиксированы;
- произвольная команда, cwd или environment не принимаются;
- отдельный `CODEX_HOME` недоступен Django user;
- user не имеет доступа к `/opt/denstock`, `.env`, DB, Docker, SSH, backup и media;
- доступ разрешён только к request directory текущего запроса;
- ownership request directory передаётся безопасным механизмом launcher;
- после процесса доступ отзывается, request directory удаляется;
- `--version`, `login status` и `exec` проверяются allowlist;
- launcher не ослабляет config overrides, sandbox или process limits.

System check требует exact handshake, `proxy_health=ok`, блокировку direct
network и точный `Logged in using ChatGPT`. API key environment, другой socket,
direct mode или неизвестный auth status блокируют production.

## Direct development command

Provider строит argv, никогда shell string. Сначала запускаются:

```text
<binary> --version
<binary> <config-overrides> login status
```

Затем:

```text
<binary> <config-overrides> exec
  --ephemeral
  --sandbox read-only
  -c approval_policy="never"
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

`<config-overrides>` для 0.142.5:

```text
forced_login_method="chatgpt"
history.persistence="none"
hide_agent_reasoning=true
show_raw_agent_reasoning=false
check_for_update_on_startup=false
web_search="disabled"
mcp_servers={}
apps._default.enabled=false
analytics.enabled=false
feedback.enabled=false
features.apps=false
features.apply_patch_streaming_events=false
features.artifact=false
features.auth_elicitation=false
features.auto_compaction=false
features.browser_use=false
features.browser_use_external=false
features.browser_use_full_cdp_access=false
features.chronicle=false
features.code_mode=false
features.code_mode_only=false
features.computer_use=false
features.current_time_reminder=false
features.default_mode_request_user_input=false
features.deferred_executor=false
features.enable_fanout=false
features.enable_mcp_apps=false
features.enable_request_compression=false
features.exec_permission_approvals=false
features.fast_mode=false
features.goals=false
features.guardian_approval=false
features.hooks=false
features.image_generation=false
features.imagegenext=false
features.in_app_browser=false
features.item_ids=false
features.local_thread_store_compression=false
features.memories=false
features.mentions_v2=false
features.multi_agent=false
features.multi_agent_v2=false
features.network_proxy=false
features.non_prefixed_mcp_tool_names=false
features.personality=false
features.plugin_sharing=false
features.plugins=false
features.prevent_idle_sleep=false
features.realtime_conversation=false
features.remote_compaction_v2=false
features.remote_plugin=false
features.request_permissions_tool=false
features.respect_system_proxy=false
features.rollout_budget=false
features.runtime_metrics=false
features.shell_snapshot=false
features.shell_tool=false
features.shell_zsh_fork=false
features.skill_mcp_dependency_install=false
features.sleep_tool=false
features.standalone_web_search=false
features.terminal_visualization_instructions=false
features.token_budget=false
features.tool_call_mcp_elicitation=false
features.tool_suggest=false
features.unavailable_dummy_tools=false
features.unified_exec=false
features.unified_exec_zsh_fork=false
features.use_agent_identity=false
features.use_legacy_landlock=false
features.web_search_cached=false
features.web_search_request=false
features.workspace_dependencies=false
features.workspace_owner_usage_nudge=false
```

Каждая строка передаётся отдельной парой `-c`, а prompt только через stdin.
Единственный включённый non-removed feature-флаг: `secret_auth_storage`,
необходимый для ChatGPT login. `resize_all_images` в 0.142.5 уже помечен как
removed/no-op и не управляет доступностью input. Image input остаётся явно
разрешён только через `--image`; image generation выключен.

## Subprocess и JSONL

Общий deadline начинается до `Popen` и охватывает spawn, неблокирующие stdin,
stdout, stderr и ожидание process. Pipe I/O обслуживается одним bounded polling
loop без reader/writer threads: каждый шаг проверяет deadline, process state,
output caps и forbidden events. Поэтому provider не может вернуть управление с
оставшимся I/O worker. На POSIX PGID сохраняется сразу после spawn. На Windows
tests используют Job Object с kill-on-close, но Windows production не
поддерживается. Process tree завершается при timeout, overflow, forbidden event
и после раннего завершения parent.

Общий stdout, stderr и незавершённая JSONL-строка имеют отдельные caps. Event
stream не сохраняется. Допустимы только lifecycle events 0.142.5, reasoning и
ровно один completed `agent_message`. Обязателен один `turn.completed`. Command,
file change, MCP, app, plugin, web search, plan и collaboration items дают
`codex_forbidden_tool_event`. Неизвестные события fail closed.

Usage принимается только как non-boolean integer в диапазоне от 0 до
1 000 000 000 для каждого поля. Обязательны `input_tokens` и `output_tokens`.
Некорректный usage даёт `codex_invalid_usage`; service дополнительно не записывает
невалидные метрики других provider в DB.

Thread ID логируется только как валидный UUID. Control characters, newline, tab
и неизвестный формат отбрасываются.

## Runtime и permissions

`CODEX_HOME` и workspace должны быть абсолютными, существующими каталогами без
symlink. Они не могут пересекаться друг с другом, repository, public/private
media или backup. На POSIX `CODEX_HOME` не доступен group/others, workspace не
доступен others. Django checks проверяют доступные ownership/mode свойства.

Каждый request directory получает mode `0700`. Schema и screenshot создаются
через exclusive open с mode `0600`. Prompt не записывается в файл. Обычный
cleanup выполняется `TemporaryDirectory` после любого результата.

Старые request directories очищаются только ручной dry-run командой:

```bash
python manage.py purge_ai_support_runtime
python manage.py purge_ai_support_runtime --confirm
```

Dry-run доступен на всех платформах. Подтверждённое удаление разрешено только на
Linux и только для непосредственных детей workspace со строгим именем
`request-*`. Команда открывает workspace через `O_DIRECTORY | O_NOFOLLOW` и
вызывает fd-relative `shutil.rmtree` только когда
`rmtree.avoids_symlink_attacks is True`. Symlink, junction, resolved target,
workspace root и соседние каталоги не удаляются. На Windows `--confirm`
завершается configuration error. Scheduler не добавлен.

## Concurrency и staging PostgreSQL

Для shared `CODEX_HOME` допустимо только
`AI_SUPPORT_CODEX_GLOBAL_CONCURRENCY=1`. DB singleton row сериализует claims
между workers, active usage row удерживает глобальный slot вне транзакции во
время provider call, а process-local semaphore является дополнительной защитой.
Slot снимается через `finally` flow при success и всех ошибках. Отдельного
отрицательного счётчика нет; capacity вычисляется по active tokens. Stale
recovery сохранён.

Перед production activation выполните на отдельной PostgreSQL test database:

```bash
DATABASE_URL=postgres://denstock_test:<password>@127.0.0.1:5432/denstock_test \
  python -m pytest tests/test_ai_support_postgresql.py -m postgresql \
  --ds=config.settings.base -q
```

Production DB для этого теста использовать запрещено.

## Retention и деградация

Приватные вложения и разговоры очищаются существующей dry-run командой:

```bash
python manage.py purge_ai_support_data
python manage.py purge_ai_support_data --confirm
```

При incompatible version, auth error, quota, timeout, malformed JSONL или
выключенном feature flag ручное обращение разработчику остаётся доступным.

## Контролируемое обновление CLI

1. Оставить feature выключенной.
2. Установить candidate version вне repository без auto-update.
3. Проверить `codex --version`, `codex exec --help` и `codex features list`.
4. Сверить каждый config override с reference candidate version.
5. Обновить pin и реальные JSONL fixtures только после compatibility audit.
6. Запустить targeted, полный pytest и PostgreSQL staging test.
7. Выполнить отдельный launcher/security review.
8. Включать feature только по новому ручному решению владельца.

Fallback на API, другой cloud provider или автоматическое ослабление config
запрещены.
