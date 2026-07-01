# План — Автоматический локальный read-only сборщик каналов Дениса

**Статус:** ПЛАН (реализацию не начинаем без отдельного «go»). Проектируем **отдельный
локальный инструмент** в `tools/research/`, который **не является частью Django-приложения
DenisStock**. Складскую систему (модели/миграции/views/templates/services) не трогаем.

> **Про доступ и безопасность (жёстко).** Инструмент только **читает публично доступный**
> контент. Он **ничего не пишет** в Telegram/YouTube: не отправляет сообщения, не комментирует,
> не ставит реакции, не подписывается, не действует от имени аккаунта. Ассистент (Claude) **не
> запрашивает и не принимает** логины, пароли, cookies, session-файлы, коды 2FA — все
> учётные данные пользователь вводит **сам, локально, в своём терминале**; ассистент их не видит
> и не хранит. Периодической слежки/автозапуска нет — запуск ручной, разовый.

---

## 1. Общая идея

Прошлый research не удалось наполнить содержанием: из среды ассистента тексты постов, названия
видео и комментарии не извлеклись. Решение — **локальный сборщик**, который пользователь
запускает у себя на машине (где есть его собственные API-credentials), выгружает **публичный**
контент в локальные файлы, обезличивает их, и только после этого ассистент анализирует **локальные
файлы** и пишет `docs/research/01…md` и `02…md`.

Разделение ответственности:
- **Сбор** (Telegram/YouTube API) — делает пользователь локально своим инструментом и своими
  ключами.
- **Анализ** — делает ассистент, читая **только** `research_inputs/denis_channels/sanitized/`.
- **В git попадают только** скрипты без секретов и итоговые `docs/research/*.md`. Выгрузки и
  секреты — **не коммитятся**.

---

## 2. Telegram collector

**Цель:** прочитать публичный канал `https://t.me/probrp1` — посты и, если доступно, публичные
комментарии из привязанной discussion-группы. Сохранить только содержательные поля, без личных
данных комментаторов.

**Технология:** **Telethon** (MTProto, официальный клиентский протокол; зрелая библиотека,
удобный read-only доступ к истории публичного канала). Альтернатива — Pyrogram (эквивалентна);
рекомендуем Telethon.

**Как работает (read-only):**
- Аутентификация через `TG_API_ID` / `TG_API_HASH` (получены пользователем на my.telegram.org).
- **Первый запуск — интерактивный вход в терминале пользователя**: Telethon сам спросит телефон +
  код (и 2FA-пароль, если включён). **Это делает пользователь; ассистент в этом не участвует и
  ничего из этого не получает.** Создаётся локальный session-файл.
- Чтение публичного канала **не требует подписки/вступления**: `client.get_entity('probrp1')` +
  `client.iter_messages(channel, limit=…)` возвращают историю постов.
- **Комментарии**: у каналов с включёнными комментариями есть привязанная discussion-supergroup;
  комментарии — это ответы в ней. Читаем через `iter_messages(discussion, reply_to=<post_id>)`
  (или `GetDiscussionMessageRequest`) **только если группа публична**. Если комментарии выключены
  или недоступны — фиксируем это в отчёте и идём дальше.
- **Только чтение**: используем исключительно `get_entity`/`iter_messages`/`get_messages`. Никаких
  `send_message`/`send_reaction`/`join`/редактирований.

**Сохраняем (для поста):** `post_id`, ссылка `t.me/probrp1/<id>`, дата (для сезонности), текст,
флаги «есть фото/видео» (без скачивания медиа), число просмотров/пересылок (если отдаёт API),
`grouped_id` (альбом). **НЕ сохраняем** медиафайлы.

**Сохраняем (для комментария):** `text`, ссылка на пост-родитель, дата (день), число реакций.
**Сразу отбрасываем PII:** имя/username/id автора, телефон, аватар — **не пишем ни в raw, ни в
sanitized** (обезличивание на этапе чтения, а не потом).

**No-credentials fallback (опционально, посты-only):** если пользователь не хочет заводить
API-credentials — есть публичный веб-предпросмотр `https://t.me/s/probrp1` (официальная
публичная страница Telegram), с которого парсятся **только тексты постов** (без комментариев).
Это запасной путь; основной — Telethon (официальный API покрывает и посты, и комментарии).

---

## 3. YouTube collector

**Цель:** по каналу `UCdgOSRp40M8rf5L9iZ4tzOg` получить список видео (названия, описания, даты) и
публичные комментарии с ответами, если включены.

**Технология:** **YouTube Data API v3** с `YT_API_KEY` (серверный API-ключ, публичные данные,
**без OAuth** — ключ физически не может писать/комментировать → инструмент read-only по природе).

**Эндпоинты и поток:**
1. `channels.list(part=contentDetails, id=UC…)` → id плейлиста загрузок (`uploads`).
2. `playlistItems.list(part=snippet, playlistId=…, maxResults=50)` (+ пагинация `pageToken`) →
   `videoId`, `title`, `description`, `publishedAt`.
3. (опц.) `videos.list(part=snippet,statistics, id=…)` → просмотры/лайки/полное описание.
4. `commentThreads.list(part=snippet, videoId=…, maxResults=100, order=relevance)` (+ пагинация) →
   тексты top-level комментариев.
5. `comments.list(part=snippet, parentId=…)` → ответы, если нужны.

**Сохраняем (видео):** `video_id`, ссылка, `title`, `description`, дата. **Сохраняем (коммент):**
`text`, `video_id`-родитель, дата (день), likeCount. **Отбрасываем PII:** authorDisplayName,
authorChannelId, авторские ссылки/аватары — **не пишем**.

**Обработка отключённых комментариев:** `commentThreads.list` на видео с выключенными
комментариями возвращает `403 commentsDisabled` — ловим, помечаем видео как «comments off», идём
дальше (не ошибка).

---

## 4. Структура локальных данных

```
research_inputs/denis_channels/          (весь каталог — в .gitignore, не коммитится)
  raw/                                    (машинные выгрузки, PII уже отброшены при записи)
    telegram_posts.jsonl
    telegram_comments.jsonl
    youtube_videos.jsonl
    youtube_comments.jsonl
  sanitized/                              (человеко-/Claude-читаемо, только содержательное, без PII)
    telegram_posts.md
    telegram_comments.md
    youtube_videos.md
    youtube_comments.md
  summaries/
    collection_report.md                 (что собрано: счётчики, ошибки, лимиты, что недоступно)
```

- `raw/*.jsonl` — по одной записи на строку; уже **без** авторских идентификаторов.
- `sanitized/*.md` — компактные списки (пост/видео: id + ссылка + дата + текст; комментарии:
  агрегировано — уникальные вопросы/темы + счётчики повторов), удобные для анализа.
- `summaries/collection_report.md` — сколько постов/видео/комментариев собрано, сколько пропущено
  (comments off / недоступно), израсходованная квота, flood-wait'ы, дата сбора.

---

## 5. Git safety (что коммитим / что нет)

Добавить в `.gitignore` (сейчас `research_inputs/`, `*.session` там **нет**; `.env.*` — уже есть):

```
# Research collector (локально, не коммитить)
research_inputs/
*.session
*.session-journal
.env.research.local
tools/research/.sessions/
```

- **Не коммитим:** `research_inputs/**` (сырые/обезличенные выгрузки), session-файлы, `.env.research.local`.
- **Коммитим только:** скрипты `tools/research/*.py` (без секретов), `requirements-research.txt`,
  `.env.research.example` (шаблон-плейсхолдер), `tools/research/README.md`, и итоговые
  `docs/research/*.md` (после анализа).
- `.env.research.local` и так попадает под существующий `.env.*`, но добавим явно для наглядности.

---

## 6. Команды (будущие)

```bash
# 1) Отдельное окружение (не трогает зависимости приложения)
python -m venv .venv-research
.venv-research/Scripts/activate            # Windows; Linux/Mac: source .venv-research/bin/activate
pip install -r tools/research/requirements-research.txt

# 2) Заполнить учётные данные (локально, не коммитится)
cp tools/research/.env.research.example .env.research.local
#   → вписать TG_API_ID, TG_API_HASH, YT_API_KEY

# 3) Сбор (read-only). Первый запуск Telegram спросит телефон/код в ВАШЕМ терминале.
python tools/research/collect_denis_sources.py --telegram --youtube
#   флаги можно по отдельности: --telegram  |  --youtube
#   опции: --limit N (сколько постов/видео), --no-comments, --posts-only (веб-fallback TG)

# 4) (опц.) Подготовка агрегата для анализа
python tools/research/analyze_denis_sources.py
```

Один orchestrator `collect_denis_sources.py` с флагами проще двух отдельных бинарей — рекомендуем его.

---

## 7. Ограничения (обязательно учесть)

- **Telegram flood-wait:** MTProto может вернуть `FloodWaitError` (нужно подождать N секунд) →
  экспоненциальный backoff, медленное чтение, лимит по умолчанию (`--limit`), уважение пауз.
- **YouTube quota:** ~10 000 единиц/сутки. `playlistItems.list`/`commentThreads.list` = 1 ед./вызов
  (до 100 элементов) → для одного канала обычно хватает; считаем израсходованное и логируем.
- **Комментарии могут быть отключены** (TG канал без discussion-группы; YT `commentsDisabled`) →
  обрабатываем как «нет данных», не как ошибку.
- **Часть постов/видео может быть недоступна** (удалены/ограничены) → пропускаем, помечаем в отчёте.
- **Медиа/видео не скачиваем** на первом этапе — только метаданные и тексты.
- **Не используем сторонний web-scraping**, если официальный API покрывает задачу (веб-предпросмотр
  TG — только опциональный fallback для постов, когда пользователь не заводит API-ключи).
- **PII не собираем**: авторов комментариев отбрасываем при записи; телефоны/адреса/аккаунты не
  сохраняем.

---

## 8. Анализ (после сбора)

- Ассистент анализирует **только** локальные `research_inputs/denis_channels/sanitized/` +
  `summaries/collection_report.md`. Сеть/сторонние сайты/общие знания о BRP — **не используются**.
- Результат — переписанные `docs/research/01-denis-public-channels-full-review.md` и
  `docs/research/02-denis-business-summary-for-owner.md` — уже **с фактическим содержанием**.
- **Каждый содержательный вывод — с основанием:**
  - `Telegram post <id>/<link>`; `Telegram comments`;
  - `YouTube video <id>/<link>`; `YouTube comments`;
  - `multiple sources` (повторяется); `not confirmed`.
- Сохраняем прежнюю маркировку достоверности (подтверждено / повторяется / единичное / не
  подтверждено / вопрос к Денису). Ничего не додумываем.

---

## 9. Что НЕ делать

- Не встраивать сборщик в Django-UI; не добавлять раздел в DenisStock; не менять складскую логику/
  модели/миграции/views/templates/services.
- Не добавлять Celery/фоновые задачи; не делать автоматическую периодическую слежку/крон.
- Не собирать персональные данные; не писать/не реагировать/не подписываться от имени аккаунта.
- Не коммитить выгрузки (`research_inputs/`), session-файлы и секреты.
- Не тащить research-зависимости в основное приложение (см. §11).

---

## 10. Файлы, которые создаются на этапе реализации

**Инструмент (коммитим — без секретов):**
- `tools/research/collect_denis_sources.py` — orchestrator (`--telegram --youtube --limit --no-comments --posts-only`).
- `tools/research/telegram_collector.py` — Telethon read-only (посты + discussion-комментарии), PII-drop.
- `tools/research/youtube_collector.py` — YouTube Data API v3 (видео + комментарии), PII-drop.
- `tools/research/sanitize.py` — `raw/*.jsonl` → `sanitized/*.md` (агрегация, обезличивание).
- `tools/research/analyze_denis_sources.py` — (опц.) частотные сводки/термины в `summaries/`.
- `tools/research/requirements-research.txt` — изолированные зависимости (см. §11).
- `tools/research/.env.research.example` — шаблон `TG_API_ID=` / `TG_API_HASH=` / `YT_API_KEY=` (плейсхолдеры).
- `tools/research/README.md` — как получить ключи и запустить (см. §12–13).
- `tools/research/tests/…` — тесты на моках (см. §14).

**Изменяется (коммитим):**
- `.gitignore` — записи из §5.

**Локальные, НЕ коммитятся:** `.env.research.local`, `research_inputs/**`, `*.session*`.

---

## 11. Зависимости и почему

| Пакет | Зачем | Примечание |
|---|---|---|
| `telethon` | чтение публичного Telegram-канала и discussion-комментариев по MTProto | Bot API не читает историю чужого публичного канала → нужен user-session; тянет `pyaes`/`rsa` автоматически |
| YouTube: **stdlib `urllib`** (без пакета) | вызовы REST YouTube Data API v3 с API-ключом | нулевая зависимость; при желании удобства — опционально `requests` |
| (тесты) `pytest` | юнит-тесты на моках | у проекта уже есть в dev-зависимостях |

**Рекомендация по размещению зависимостей — отдельный `tools/research/requirements-research.txt`,
НЕ в `pyproject.toml`.** Причины:
- `telethon` — сторонний тяжеловесный клиент, не нужный складскому приложению ни в dev, ни в
  проде; его не должно быть в образе Docker и в install-графе приложения (меньше размер и attack
  surface, чистый prod).
- Research — разовый локальный инструмент; ставится в **отдельный** `.venv-research`, не смешиваясь
  с зависимостями DenisStock. Это соответствует принципу минимальных зависимостей проекта
  (как CSV вместо openpyxl, Code128-SVG вместо python-barcode, FileField вместо Pillow).

---

## 12. Как пользователь получает ключи (ассистент их не видит)

**Telegram `TG_API_ID` / `TG_API_HASH`:** зайти на `my.telegram.org` → *API development tools* →
создать приложение → скопировать `api_id` и `api_hash` в `.env.research.local`. Первый запуск
сборщика попросит телефон и код входа (и 2FA-пароль, если есть) **в терминале пользователя** —
это личные данные, ассистенту их передавать **не нужно и нельзя**.

**YouTube `YT_API_KEY`:** Google Cloud Console → создать проект → *Enable APIs* → включить
**YouTube Data API v3** → *Credentials* → *Create credentials* → *API key* → вписать в
`.env.research.local`. OAuth не требуется (только публичные данные).

> Ассистент никогда не просит вставлять эти значения в чат. Они живут только в
> `.env.research.local` на машине пользователя (gitignored).

---

## 13. Как запустить (кратко)

1. `python -m venv .venv-research` и `pip install -r tools/research/requirements-research.txt`.
2. `cp tools/research/.env.research.example .env.research.local` и заполнить ключи.
3. `python tools/research/collect_denis_sources.py --telegram --youtube` (первый TG-запуск —
   интерактивный вход в вашем терминале).
4. Проверить `research_inputs/denis_channels/summaries/collection_report.md`.
5. Дать ассистенту знак — он проанализирует `sanitized/` и обновит `docs/research/*.md`.

---

## 14. Тесты без реального доступа к API

Всё на **моках/фикстурах, без сети** (`tools/research/tests/`):
- **Telegram:** фейковые message-объекты (текст, дата, флаг медиа) → collector пишет `jsonl` с
  разрешёнными полями и **без** авторских идентификаторов.
- **YouTube:** сохранённые JSON-фикстуры ответов `playlistItems`/`commentThreads` → корректный
  парсинг, пагинация по `pageToken`, обработка `commentsDisabled`, PII-drop.
- **sanitize:** на входе запись с авторскими полями → на выходе в `sanitized/` **нет** имени/id
  автора; тексты сохранены; агрегация повторов считается верно.
- **Безопасность конфигурации:** `.gitignore` содержит `research_inputs/`, `*.session`,
  `.env.research.local`; проверка, что сборщик не вызывает никаких write-методов (только
  read-API в списке разрешённых).
- **Оффлайн-гарантия:** тесты не ходят в сеть (все клиенты замоканы).

---

## 15. Риски

- **Telegram login-барьер:** Telethon требует user-session (телефон+код). Митигируем: интерактивный
  вход делает пользователь локально; для нежелающих — веб-fallback `t.me/s/` (посты-only).
- **Flood-wait / quota:** backoff, лимиты, учёт квоты, понятные сообщения; сбор разовый.
- **Комментарии/посты могут быть недоступны** (off/удалены) → graceful skip + отметка в отчёте.
- **PII-утечка:** снимаем отбрасыванием авторов при записи (не только в sanitized).
- **Смешение зависимостей:** изолируем в `.venv-research` + `requirements-research.txt`.
- **Случайный коммит выгрузок/секретов:** закрываем `.gitignore` (§5) + тест на игнор.
- **ToS/этика:** только публичное, read-only, уважение rate-limit, без массового скачивания медиа.

---

## 16. Что будет закоммичено сейчас и потом

- **Сейчас (этот шаг):** только этот план — `docs/plans/31-denis-auto-research-collector.md`.
  Коммит: `План: автоматический сбор каналов Дениса`.
- **На этапе реализации (после «go»):** скрипты `tools/research/*` + `requirements-research.txt` +
  `.env.research.example` + README + тесты + правки `.gitignore` (коммит вроде
  `Research tooling: локальный read-only сборщик`). Выгрузки и секреты — **не** коммитятся.
- **После сбора пользователем:** обновлённые `docs/research/01…md` / `02…md` с фактическим
  содержанием и основаниями.
