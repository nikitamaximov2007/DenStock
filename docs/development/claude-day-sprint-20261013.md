# Claude: дневной спринт 2026-10-13 (передача работы)

Production не менялся: web `f7b80861`, catalog-web образ 2026-09-28, telegram-bot
`952ee8eb`, max-bot `1f6dc57c`, миграций в ожидании 0, Caddyfile `8ba9fb9e`,
`/media/customer_requests/` и вариант в верхнем регистре дают 404. `origin/main`
`bc53d89` является предком production.

## Сделано

| Фаза | Ветка | SHA | Итог |
| --- | --- | --- | --- |
| P1 GitHub-копии: аудит и исправления | `claude/github-release-backups` | `c40d885` | 35 + 3 теста, приемка на настоящем GitHub подготовлена |
| P2 private_media: PG16, preflight | `claude/private-media-backup-release` | `2405d91` | 168 PG16 / 172 SQLite, предпроверки production пройдены |
| P3 Caddy: настоящий Caddy 2 | `claude/private-attachment-security-live-caddy` | `8d6fbba` | 15 вариантов обхода дают 404, без защиты 13 из них утекают; перенос вложений: fsync новых каталогов |
| P3 права `denstock_public` | `claude/public-role-least-privilege` | `b1498ad` | себестоимость, оптовые цены, телефоны резервов больше не читаются; PG16 и полный SQLite без новых падений |
| P4 откат выпуска 2 Codex | `claude/codex-r2-rollback-db-defaults` | `4402df9` | доказано на PG16: без исправления откат на f7b8086 ломает старт пересчета; с исправлением работает |

## Важные выводы

- catalog-web (образ 2026-09-28) отличается от f7b8086 в 43 файлах, но для
  публичного каталога только `apps/catalog/search.py`, и только необязательным
  `active_only` (его использует внутренний выбор детали). Пересборка
  catalog-web на f7b8086 публичное поведение не меняет.
- SEO-кандидат `99bf877` и права по столбцам совместимы при одной строке:
  `_compute_in_stock_public_parts` должен вызывать
  `live_stock_rows(public_columns_only=True)`. Проверено слиянием локально
  (171 тест PG16), не опубликовано: выпуски раздельные.
- Тест PG16 private_media не передавал пароль в pg_restore (исправлено в RC).

## Текущая фаза

P5/P6: SEO проверен (98 SQLite / 169 PG16); далее DR: тест Docker-тома с
символической ссылкой поколения.

## Ждут владельца

- Ввод парольной фразы для `scripts/dr/morning_real_drill.sh` (ветка
  `claude/offline-full-service-restore`).
- Выпуск private_media (только `web`), затем репозиторий и токены GitHub.
