# ДенисStock - ночной handoff

Обновлено: 2026-09-21

## Авторитетная линия

- Production и `origin/main` после отдельного Customer Cabinet release: `d532ac565628ec0da4541a47f647dd83de5e4248`.
- Customer Cabinet установлен в production, но выключен: `CUSTOMER_MESSENGER_CABINET_ENABLED=false`, `CUSTOMER_ACCOUNT_ENABLED=false`, `CUSTOMER_AUTH_MAX_ENABLED=false`.
- Mobile Operator Console разрабатывается только в worktree `/tmp/denstock-mobile-operator-console-v1`, ветка `codex/mobile-operator-console-v1`.
- `CUSTOMER_OPERATOR_CONSOLE_ENABLED` по умолчанию `false`; production не изменялся после Customer Cabinet release.

## Mobile Operator Console V1

Реализованы additive-модели и явная привязка `provider + provider_user_id`:

- одноразовый хешированный pairing token с TTL и fail-closed конфликтами;
- активные Telegram/MAX binding-и и немедленный отзыв доступа;
- режимы `/work` и `/customer`, bounded «Все заявки»/«Новые заявки»;
- внутренний request card, active request context и повторная авторизация перед действием;
- durable idempotent notifications на каждую заявку и binding;
- единый `operator_replies` routing в transport заявки;
- immutable customer-visible author snapshot и operator control source;
- восстановление прерванных operator notifications в `uncertain`, без вечного `sending`;
- общий private attachment validator/storage pipeline для text + PNG/JPEG/WEBP/PDF operator reply;
- generated migrations `0017`, `0018`, `0019`.

## Проверки

- `manage.py check`: PASS.
- `manage.py makemigrations --check`: PASS.
- compileall: PASS.
- focused operator, Telegram and MAX suites: PASS after the final RC changes; four skipped attachment-related cases are inherited from the existing suite.
- `ruff check .`: PASS; `djlint templates/customer_requests/staff_bindings.html --check`: PASS; `git diff --check`: PASS.
- Full baseline/candidate smoke completed with the three known inherited failures listed below; the candidate added no new stable failure. The operator-focused run completed with zero failures.

## Qualification caveats

Fresh full-suite inherited failures:

- `tests/test_partial_repair_line_cancellation.py::test_report_button_confirm_screen_and_redirect_keep_filters`
- `tests/test_max_bot_compose.py::test_max_bot_mounts_only_the_public_ca_directory_read_only`
- `tests/test_max_edge_route.py::test_only_the_public_catalog_block_changes`

The random-token assertion in `tests/test_search.py::test_cost_hidden_for_storekeeper` passed when rerun and is not an RC regression.

## Утреннее включение

Не включать автоматически. Сначала owner должен отдельно привязать сотрудников через admin-only `/customer-requests/staff-bindings/`:

1. администратор выбирает внутреннего сотрудника, provider и customer-visible label;
2. сотрудник отправляет одноразовый `pair_...` код в соответствующий бот;
3. сотрудник отправляет `/work` и проверяет заявку; `/customer` возвращает обычный customer mode;
4. только после независимого review можно включать `CUSTOMER_OPERATOR_CONSOLE_ENABLED=true`.

Не записывать реальные provider IDs в исходный код и не включать флаги во время ночного сеанса.

## Открытые проверки

- Требуется отдельная человеческая проверка мобильного operator flow в Telegram и MAX после code review.
- Требуется подтвердить фактический production payload входящих MAX вложений перед включением: код поддерживает официальный URL/base64 adapter, но feature остаётся выключенной.
- Production deployment Mobile Operator Console этой ночью не выполнялся.
