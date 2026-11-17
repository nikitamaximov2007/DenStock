# Handoff: финальная приёмка Telegram/MAX и production release

Ветка `codex/prostor-bots-integration`; текущий HEAD записан в `git log`.
Исходный RC был `c0922c94c2bb20b4d9d79601d7356c5ea59b27a1`. Для сохранения
production Turkey reverse SSH исправления `e05ebe80d77673c5672d93967972c0685cf91744`
оно влито в кандидата. Release-helper также обновлён на актуальные миграции
0029/0030 и проверку существующего bounded Caddy webhook route. Эти правки
требуют нового финального SHA и новой image identity сборки. Production пока не
изменялась.

## Завершено

- Обновлены устаревшие ожидания Caddy и Compose в тестах и release qualification.
- Профильный набор Compose/Caddy: 18 passed; Caddy route checker прошёл.
- `ruff check .`, `djlint templates --check`, `manage.py check` и
  `makemigrations --check` прошли.
- Целевые интеграционные тесты: `375 passed, 5 skipped`.
- Полный pytest: `6743 passed, 298 skipped, 7 failed`. Все семь точных test ID
  повторены на baseline `ba12a3d`: три теста пустой цены, ремонтная кнопка,
  два теста sitemap lastmod с UTC/local date boundary и root-owned AI secret.
  Полный набор не зелёный, новых падений нет.
- `ruff check .`, `djlint templates --check`, `manage.py check` и
  `makemigrations --check` ранее прошли; после текущих docs-only изменений
  повторить перед выпуском.
- DEV web и MAX worker работают из образов `c0922c94...`. Все commits after
  `c0922c94` contain only tests/docs/handoff. Пересобрать production release
  images из окончательного HEAD; старые artifact checksums не использовать.
- Владелец подключил свой разрешённый MAX-клиент к искусственной DEV-заявке №1.
  DEV webhook сохранил одно сообщение `DEV MAX сообщение после привязки 18.11`
  с внешним ID в правильной связанной переписке. Автор совпадает с активной
  привязкой MAX.
- Владелец подтвердил, что веб-ответ `DEV MAX ответ из веб-интерфейса 18.11`
  показан в разрешённом MAX-диалоге ровно один раз. В DEV он сохранён со статусом
  `sent` и внешним ID. После перезапуска MAX worker обе записи остались по одной,
  worker восстановил healthy heartbeat; свежих ошибок в логах нет.
- Из ранее согласованного фото DEV-заявки №2 отправлен один ответ `Тестовое фото
  DEV RC` в ту же связанную заявку №1. DEV запись имеет вложение в
  `PrivateAttachmentStorage`, статус `sent` и внешний ID. После второго
  перезапуска MAX worker сообщение осталось единственным, ошибок нет.
  Фактическое получение владельцем не подтверждено.
- DEV использует БД `denstock_rc`; MAX webhook включён; operator console и
  introductions выключены; активных тестовых привязок сотрудников нет.
- Временный sidecar для проверки introductions остановлен и удалён; его тома
  не затрагивались. Основные DEV-контейнеры продолжают работать.
- Production и Telegram production не изменялись.

## Продолжение 2026-11-18

- Read-only production: MAX `max_bot_health` passed, `max_bot_identity` вызвал
  успешный read-only API запрос, подписка требует и содержит правильный
  webhook path, POST без секрета к loopback-Caddy получил 404. Live Caddy
  configuration прошла `caddy validate`; существующий ограниченный маршрут
  не менялся.
- Production Telegram container healthy, штатный heartbeat свежий, `getMe`
  через настроенный Turkey egress успешен. `denstock-telegram-egress.service`
  active. DB и остальные production сервисы healthy.
- Целевые MAX/Telegram/media/workspace/release-tooling проверки: `378 passed,
  5 skipped`; Turkey egress набор отдельно `16 passed`. Полный pytest после production merge:
  `6743 passed, 298 skipped, 7 failed`; это семь ранее воспроизводимых
  baseline failures. Полный набор не объявлять успешным.
- Исправлены migration expectations release-helper (0029/0030) и Caddy
  preflight: он теперь проверяет метод, точный webhook path, 64 KB и
  `web:8000`, не требуя заменить существующий production Caddyfile.
- `catalog-web` нужен для новой MAX handoff-кнопки. Он отсутствует в явном
  разрешённом списке production-компонентов; владельцу задан вопрос о включении.
  До решения не создавать PRE-backup и не менять production.
- Исходящая фотография в DEV имеет `sent` и внешний ID, но реальное получение
  владельцем не подтверждено. Оставить это ограничение в отчёте.
- Перед deployment повторить профильные/static gates, завершить commit, собрать
  точные linux/amd64 образы из итогового SHA и проверить hash/image ID. Затем
  только при GO выполнить свежий signed PRE, offsite read-back и isolated
  restore. Не менять Caddy, production MAX subscription, secret files, catalog
  или egress без отдельной согласованной области работ.

## Не завершено / блокеры

- Полная живая приёмка исходящей фотографии владельцем не подтверждена. Это
  должно остаться явным ограничением, если владелец не ответит.
- Кандидатные integration tests проверяют приватную фотообработку, отказы при
  недоступном файле, idempotency, две разные клиентские личности, роли операторов,
  системные представления и старые/новые callback. Эти тесты не равны двум
  реальным клиентским MAX-аккаунтам или личным identities Дениса/Рима.
- В DEV operator flags выключены; текущий DEV не проверял включённые представления
  реальных сотрудников. Не выдавать тестовые фикстуры за личные identities.
- Доступность production SSH, состояние текущих migrations/secrets/webhook,
  новый подписанный PRE-backup и rollback gate ещё не проверены.
- `release_image_identity.py verify` из worktree без runtime `.env` не использовать;
  не создавать `.env` из production secrets. Повторно запустить `build` на
  финальном SHA и проверить identity по подготовленному release bundle согласно
  production runbook.

## Следующие шаги

1. Завершить read-only production preflight по утверждённому runbook без утечки
   секретов, проверить точный текущий SHA/images/migrations, Telegram egress,
   production MAX webhook, uncertain queues, ресурсы и rollback.
2. Проверить production operator readiness. Не включать operator flags без
   активных подтверждённых bindings и прохождения штатной readiness команды.
3. Повторить static checks и image identity build из финального HEAD; проверить
   deploy sequence не обновляет неразрешённый catalog/Caddy/egress компонент.
4. Провести свежий подписанный PRE-backup с PostgreSQL, public/private media,
   manifest, hashes, offsite read-back и изолированным restore. При любом
   несоответствии остановиться до rollout.
5. Выполнить разрешённый последовательный rollout только `web`,
   `telegram-bot`, `max-bot` и необходимые migrations, с rollback по component
   image IDs. Не перезапускать остальные сервисы.
6. Выполнить production health checks без отправки клиентских сообщений.
7. Зафиксировать явные ограничения ручной MAX приёмки, в том числе отсутствие
   подтверждения исходящей фотографии, если оно так и не поступит.

## DEV/секреты

- MAX test bot и webhook остаются DEV-only; ngrok/ngrok webhook отключать после
  завершения DEV-проверок и удаления временной тестовой подписки, отдельно от
  production MAX webhook.
- Не записывать в handoff токены, pairing-коды, чат ID, внешние URL вложений,
  пароли или содержимое реальных переписок.
- Основной DEV web и MAX worker используют образы `c0922c94...`; новые образы
  `8c767d47...` собраны, но ещё не запущены в RC. Рабочее дерево следует
  проверить перед продолжением.
