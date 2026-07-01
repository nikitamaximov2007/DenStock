# Denis public channel research collector

Локальный read-only collector для публичных источников Дениса:

- Telegram: `https://t.me/probrp1`
- YouTube channel id: `UCdgOSRp40M8rf5L9iZ4tzOg`

Инструмент не является частью Django-приложения DenisStock. Он не меняет модели,
views, templates, services, миграции или складскую бизнес-логику.

## Read-only правила

Collector только читает публичный контент. Он не отправляет сообщения, не
комментирует, не ставит реакции, не подписывается, не вступает в группы и не
использует browser cookies.

Telegram login, если Telethon запускается впервые, происходит только локально в
терминале пользователя. Не передавайте ассистенту телефон, коды, пароли, 2FA,
session-файлы, cookies или API keys.

## Установка

```powershell
python -m venv .venv-research
.venv-research\Scripts\activate
pip install -r tools/research/requirements-research.txt
Copy-Item tools\research\.env.research.example .env.research.local
```

Заполните `.env.research.local` локально:

```text
TG_API_ID=
TG_API_HASH=
YT_API_KEY=
```

YouTube использует только YouTube Data API v3 API key, без OAuth. Telegram
использует Telethon user-session, который хранится в `tools/research/.sessions/`
и игнорируется git.

## Dry run и help

```powershell
python tools/research/collect_denis_sources.py --help
python tools/research/collect_denis_sources.py --telegram --youtube --dry-run
```

Dry run не делает network calls и не пишет файлы.

## Сбор

```powershell
python tools/research/collect_denis_sources.py --telegram --youtube
```

Полезные ограничения:

```powershell
python tools/research/collect_denis_sources.py --telegram --youtube --limit 30 --comment-limit 50
python tools/research/collect_denis_sources.py --telegram --youtube --posts-only
python tools/research/collect_denis_sources.py --sanitize-only
```

Все выгрузки пишутся только в:

```text
research_inputs/denis_channels/
```

Структура:

```text
research_inputs/denis_channels/
  raw/
  sanitized/
  summaries/collection_report.md
```

`raw/*.jsonl` уже пишутся с whitelist полей и без author identifiers. `sanitized/*.md`
дополнительно форматируются для анализа. Телефоны, email, Telegram usernames и
личные links внутри текста заменяются на markers:

- `[phone redacted]`
- `[email redacted]`
- `[username redacted]`
- `[link redacted]`

## Локальный анализ

```powershell
python tools/research/analyze_denis_sources.py --top 50
```

Analyzer читает только `research_inputs/denis_channels/sanitized/` и пишет
summary обратно в `research_inputs/denis_channels/summaries/`.

## Тесты research tool

Тесты не ходят в сеть и используют mocks/fixtures:

```powershell
pytest tools/research/tests
```
