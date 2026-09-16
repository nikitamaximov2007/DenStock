# PRO-STOR requests: Telegram and MAX live-enable runbook

What exists today, what switching each channel on means, and how to check
and undo it. No credential is stored in Git; the values below are names of
settings, not secrets.

## What exists in the code

* A customer picks Telegram or MAX on the request form. The request works
  without either channel being configured: operators see the phone number
  and the chosen messenger in "Заявки клиентов".
* Telegram linking (`apps/customer_requests/messengers.py`, `telegram.py`):
  from a request card an operator issues a one-time link
  `https://t.me/<bot>?start=<token>` (token stored only as SHA-256, valid for
  `TELEGRAM_REQUEST_LINK_TTL_SECONDS`, 60 s to 7 days, default 1 day) and
  sends it to the customer. When the customer presses Start, Telegram calls
  the webhook on the INTERNAL host, `/customer-requests/telegram/webhook/`,
  and the request records the chat id. Wrong or missing secret: 404. Bad
  JSON: 400. Unknown, expired or used token: accepted = false.
* Telegram messaging is live in production: the bot sends the request summary
  after Start, carries customer messages to operators and operator replies
  back, and records every message with the real DenisStock operator behind it.
* One chat serves several requests. The chat's active conversation decides
  where a plain message goes; `/requests` lets the customer switch. When more
  than one request is possible and none is active, the bot asks instead of
  guessing.
* A deep link binds a request on the customer's *first* start. A returning
  customer who already has the chat selects the request instead of opening a
  new link — the platform delivers no start payload into an existing dialog.
* MAX: the transport is implemented and accepted locally against a fake MAX
  (webhook, worker, returning customers, operator replies from DenisStock,
  success-page handoff). It is **not deployed**: no token, no subscription, no
  `max-bot` service on production yet. Design and guarantees:
  `docs/design/customer-request-max.md`.

## Telegram: switch on (internal runtime only)

1. Owner: create the bot with @BotFather; keep the bot token in the secrets
   store (it is not used by DenisStock today, only for registering the
   webhook).
2. Internal `.env` (not `.env.public`):
   * `TELEGRAM_BOT_USERNAME=<bot username without @>`
   * `TELEGRAM_WEBHOOK_SECRET=<long random value>`
   * optional `TELEGRAM_REQUEST_LINK_TTL_SECONDS` (60 to 604800)
3. Recreate the internal `web` in a normal release window
   (`docker compose up -d --no-deps web`).
4. Register the webhook from a workstation, with the bot token from the
   secrets store: Bot API `setWebhook` with
   `url=https://<internal host>/customer-requests/telegram/webhook/` and
   `secret_token=<TELEGRAM_WEBHOOK_SECRET>`. Telegram then sends the secret
   in `X-Telegram-Bot-Api-Secret-Token`.

## Telegram: acceptance

* `curl -s -o /dev/null -w '%{http_code}' -X POST https://<internal host>/customer-requests/telegram/webhook/`
  answers 404 (no secret).
* On a test request with Telegram chosen: "Создать ссылку Telegram" shows a
  `t.me` link; opening it and pressing Start marks the card "Telegram:
  связан"; opening the same link again changes nothing.
* The same flow was exercised locally with a mocked update (wrong secret,
  bad JSON, wrong token, valid start, replay, expired token), see
  `docs/qualification/public-catalog-final-night-review.md`.

## Telegram: roll back

1. Bot API `deleteWebhook`.
2. Remove `TELEGRAM_WEBHOOK_SECRET` (the webhook then answers 404 to
   everything) and `TELEGRAM_BOT_USERNAME` (the link button reports that
   the bot is not configured), recreate `web`.
3. Recorded chat ids stay on their requests; nothing else changes.

## MAX

Implemented, not switched on. Until Stage C switches it on, catalog-web has
no `MAX_BOT_USERNAME`, so a customer who chooses MAX sees that MAX is
unavailable and is called back by phone; the request itself is always kept.

### Services and secrets (target for Stage C)

* `catalog-web`: `MAX_BOT_USERNAME` (the platform-generated nickname) and
  optionally `MAX_DEEP_LINK_BASE_URL` (default `https://max.ru`). No secret;
  `config/settings/public.py` empties any that leak in.
* `web`: `MAX_WEBHOOK_ENABLED=true`, `MAX_WEBHOOK_SECRET` (5 to 256 characters
  `[A-Za-z0-9_-]`). No bot token.
* `max-bot` (runs `manage.py run_max_bot`, one instance): `MAX_BOT_TOKEN` from
  its own root-owned mode 600 env file, never in Git and never printed;
  `TELEGRAM_INTERNAL_BASE_URL` for the «Открыть заявку» button.
* `telegram-bot` delivers the employees' notifications about MAX requests, so
  it must run the same release.

### Switch on (Stage C, production)

1. Standard release with backups; migrations `customer_requests 0008, 0009`
   and `operations 0006`; re-run `create_public_catalog_role.sql` (idempotent).
2. Publish exactly `https://<host>/customer-requests/max/webhook/` on port 443
   with a trusted certificate, proxied to `web`. Nothing else of `web`.
3. Set the variables above; recreate `web`, `catalog-web`, `telegram-bot`;
   start `max-bot`.
4. `manage.py max_webhook status`, then
   `manage.py max_webhook subscribe --confirm` with `MAX_PUBLIC_WEBHOOK_URL`,
   `MAX_WEBHOOK_SECRET` and `MAX_BOT_TOKEN` in that one-off environment.

### Acceptance

* `curl -s -o /dev/null -w '%{http_code}' -X POST https://<host>/customer-requests/max/webhook/`
  answers 404 (no secret).
* A real MAX account: request with MAX, «Продолжить в MAX» opens the bot,
  «Начать» sends the summary; first message gets one acknowledgement, the
  second none; an employee answers from the request page and the customer
  receives it once; a second request is bound in the same dialog and chosen
  with the buttons; a cancelled request cannot be bound.
* Locally the same flows run against `tests/max_fake.py`
  (`python -m tests.max_fake serve` / `send`).

### Roll back

1. `manage.py max_webhook unsubscribe --confirm`.
2. `MAX_WEBHOOK_ENABLED=false` on `web` (the webhook answers 404), stop
   `max-bot`, empty `MAX_BOT_USERNAME` on `catalog-web`.
3. Stored conversations and messages stay; the migrations are additive.
