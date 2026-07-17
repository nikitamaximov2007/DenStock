# ИИ-поддержка: архитектура и эксплуатация

## Граница безопасности

`apps.ai_support` является отдельным read-only приложением. HTTP views вызывают
только собственный service layer, provider interface, локальный лексический
retrieval и сборщик безопасной диагностики. У provider нет tools, function
calling, SQL, shell, web search и доступа к изменяющим сервисам DenisStock.
Ответ сохраняется и выводится как обычный escaped text.

Prompt injection нельзя исключить полностью. Основная защита обеспечивается
архитектурной границей: модель может вернуть только текст и не получает
исполняемых инструментов. System instruction разделяет доверенные правила,
проверенные справочные фрагменты и недоверенные сообщения, историю и изображения.

Никогда не передаются cookies, session/CSRF/Authorization tokens, произвольные
headers, query string, environment, SQL, логи, дампы БД, email пользователя,
system prompt как сохранённые данные или полные provider payloads.

## Включение provider

Функция по умолчанию выключена. Для OpenAI задаются только через environment:

```text
AI_SUPPORT_ENABLED=true
AI_SUPPORT_PROVIDER=openai
AI_SUPPORT_MODEL=<утверждённая модель>
AI_SUPPORT_API_KEY=<секрет из secret storage>
DENSTOCK_PUBLIC_BASE_URL=https://185-250-44-206.sslip.io/
DENSTOCK_APP_COMMIT=<release SHA>
```

Ключ нельзя помещать в HTML, JavaScript, БД, git или логи. Адаптер использует
Responses API, `store=false`, `max_retries=0`, общий timeout и не передаёт tools.
Fake provider заблокирован в обычных settings и разрешается только тестами.

Перед production необходимо отдельно утвердить provider account, модель,
правила обработки данных провайдером, расходы, канонический URL и значения квот.

## Knowledge и retrieval

Production retrieval читает только allowlisted Markdown из
`apps/ai_support/knowledge_pack/`. Он детерминированный, без сети, выбирает не
более четырёх фрагментов общим размером до 6000 символов. Репозиторий, тесты,
старые design docs и operational secrets не индексируются.

## Приватные изображения

Разрешено одно JPG, PNG или WEBP изображение до 5 МБ и 20 мегапикселей.
Pillow полностью декодирует файл, запрещает анимацию и несколько кадров,
удаляет метаданные повторным кодированием и сохраняет файл под UUID. Исходное
имя не сохраняется. Содержимое картинки не проходит OCR, поэтому пользователь
обязан самостоятельно исключить видимые пароли, cookies, API keys и токены.

`PRIVATE_MEDIA_ROOT` не должен совпадать с `MEDIA_ROOT`. В Docker named volume
`private_media` подключён только к `web`; Caddy подключает только обычный
`media`. Файл выдаёт authenticated Django view с ownership или manager ticket
capability, `Cache-Control: private, no-store` и `nosniff`.

## Права и обращения

`use_ai_support` получают все рабочие роли. `manage_ai_support_tickets` получают
Администратор и Руководитель. Каждый view проверяет capability на сервере.
Обычный пользователь видит только свои разговоры и вложения. Менеджер не
получает доступ к разговору: ticket содержит только явно выбранный вопрос,
ответ, скриншот и allowlisted diagnostic snapshot.

## Квоты и ошибки

DB-backed `SupportUsageDay` ограничивает короткий интервал, сутки, фактический
token usage и один активный запрос пользователя. Сетевой вызов выполняется вне
DB transaction. Зависший lock освобождается после безопасного stale interval.
Timeout, 429, 5xx, invalid response, concurrent request и quota exceeded
показываются без stack trace; ручное обращение остаётся доступным.

## Retention и очистка

Значения по умолчанию: вложения 30 дней, разговоры и обращения 180 дней.
Команда всегда начинает с dry-run:

```bash
python manage.py purge_ai_support_data
python manage.py purge_ai_support_data --confirm
```

Первый вызов только показывает количество записей и файлов. Второй удаляет
только найденные записи ИИ-поддержки и файлы внутри `PRIVATE_MEDIA_ROOT`.
Scheduler в MVP не добавлен.

Текущий offsite backup pipeline не изменён и приватные скриншоты автоматически
в него не включены. Backup policy, шифрование и срок хранения приватного тома
требуют отдельного решения до production.

## Диагностика

Route context принимается только как path без query/fragment, проверяется через
Django `resolve` и allowlist route names. Browser family и viewport жёстко
нормализуются. App commit читается из `DENSTOCK_APP_COMMIT`; HTTP request не
запускает git. В логах допустимы только UUID, user id, provider/model, status,
latency, числовой usage, безопасный request id и safe error code.

Если provider выключен или не настроен, проверьте feature flag, provider/model,
наличие секрета в environment и канонический URL. Не выводите значения секретов
в диагностику. Проверку реального provider следует выполнять только отдельным
ручным решением после настройки privacy и расходов.
