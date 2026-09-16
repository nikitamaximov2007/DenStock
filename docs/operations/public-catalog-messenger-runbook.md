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
* MAX: the domain boundary exists (`max_provider.py`) and the official API
  contract is verified and written down in
  `docs/design/customer-request-max.md`. The live transport, webhook route and
  credentials are staged work, not yet in the runtime.

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

Not switchable yet, but no longer unknown: the official contract is verified
and recorded in `docs/design/customer-request-max.md` (host
`platform-api2.max.ru`, `Authorization` header, webhook-only in production via
`POST /subscriptions` with `X-Max-Bot-Api-Secret`, deep link
`https://max.ru/<botName>?start=<payload>` with a 128-character payload).

Still missing before MAX can be switched on: the live transport client, the
webhook route, and the bot token installed on production the same way the
Telegram one is — root-owned, mode 600, never in Git and never printed. Until
then customers who choose MAX are contacted by phone or from the staff
member's own MAX account.
