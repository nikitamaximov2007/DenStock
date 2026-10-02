# Telegram ↔ MAX bot: business-function parity audit

Baseline: `origin/main` 7ad045b (fetched 2026-10-02). Candidate branch: `claude/bot-parity-audit`.
Reference behaviour: the Telegram adapter. Executable proof: `tests/test_bot_business_parity.py`
runs every scenario unchanged against both real adapters and asserts database state.

## Architecture found

| Layer | Telegram | MAX | Shared |
|---|---|---|---|
| Transport | `telegram_bot` (long polling, offset commits with the update) | `max_bot` + `views.max_webhook` (webhook, may redeliver) | |
| Customer rules | `telegram_service` | `max_service` | `messaging`, `customer_ui`, `customer_cabinet`, `messengers` (links), `customer_accounts.messenger_hooks` |
| Staff console (paired `StaffMessengerBinding`) | adapter glue only | adapter glue only | `operator_console` (lists, cards, reply mode, part photos), `operator_replies`, `catalog.photo_pipeline` |
| Legacy operators' inbox (`TelegramOperator`) | `telegram_service` + `operator_bot` | none: MAX requests are announced through this same Telegram bot (`MaxOperatorDelivery`) | `operator_bot`, `operator_replies` |

All staff business logic is already one shared service; parity risk sits in the adapters.
Neither bot offers part search, stock, price, cell or analog lookups: there is no such
Telegram capability to mirror.

## Defects confirmed and fixed (MAX adapter only)

1. **Staff answers undeliverable in MAX.** Console answers were stored as
   `{"inline_keyboard": rows}`; the MAX sender iterates rows and raised `TypeError`, so a MAX
   employee received no menu, card, reply prompt or photo prompt after the first panel. The
   same bug had been fixed for owner-panel notifications only (c1ee2d2). Fix: store native rows;
   the sender also unwraps rows queued in the old shape.
2. **Paired staff could press customer buttons in MAX** (requests selector, purchases, reorder
   confirmation). Telegram refuses them for an active staff identity. Fix: refuse in MAX too.
3. **Redelivered MAX staff events were applied again** (console state transitions such as
   «Добавить ещё фото», and a resent pairing code fell into the customer flow). Telegram applies
   every update once. Fix: a staff event whose stored answer exists is not re-applied.
4. **Typed «Мои заявки» / «Мои покупки» (and internal button labels typed by a customer)** were
   stored in MAX as a message to the service; Telegram treats them as navigation. Fix: same
   navigation in MAX.
5. **Customer attachments**: MAX stored the caption and silently dropped the photo or file;
   Telegram refuses the whole message. Fix: same refusal in MAX.
6. **Non-photo MAX attachments** (sticker, location, share) could be read as a staff photo;
   Telegram reads only a photo or a document. Fix: MAX reads only `image` and `file`.

## Telegram issue reported, not changed

A paired employee who sends a pairing code again (or a fresh code for the same identity) gets
the customer greeting, in both messengers, because `operator_console.handle_text` returns
`None` for a refused code. No data changes. MAX redelivery no longer reaches it (fix 3).

## Legitimate platform differences

* MAX has no persistent reply keyboard: the staff panel and «Мои заявки» ride on inline buttons.
* MAX callback answers redraw the pressed message; Telegram sends a new message.
* MAX may carry several attachments in one message: the first `image` or `file` is used (one
  event, one receipt). A Telegram album arrives as separate updates; after the first photo the
  others are refused with «Сначала выберите действие для фото». The part ends with one new
  photo in both.
* The legacy `TelegramOperator` inbox is Telegram-only by product decision (confirmed by the
  owner on 2026-10-02): it stays a legacy Telegram surface and is not ported to MAX. MAX staff
  use the shared operator panel, which provides the same business capabilities: request list,
  request card, reply to the customer, attachments, the part-photo workflow, navigation and
  cancel.

## Release qualification notes

* Rows already queued in production in the old `{"inline_keyboard": rows}` shape are sent as
  valid MAX keyboards by the fixed sender, and the next queued row is still delivered: no data
  cleanup is needed (`test_old_envelope_rows_already_queued_are_sent_and_the_sender_continues`).
* Residual, out of scope (reliability phase 2): an unexpected non-MAX exception inside the MAX
  sender still stops the worker loop; the known button TypeError is removed.
