# Telegram + MAX bot workers: reliability audit

Baseline: `origin/main` 66debcc (2026-10-02). Candidate branch:
`claude/bot-worker-reliability-audit`. Executable proof:
`tests/test_bot_worker_reliability.py` (26 of its 27 tests fail on the baseline; the
remaining one pins the database-fault path that was already correct).

Property: an isolated bad update, row or API answer can fail, but it cannot stop
unrelated Telegram or MAX processing, and it never ends as `sent`.

## Execution maps

Telegram (`run_telegram_bot`, one process, long polling):
`run` > `iterate` > `poll_once` (per update: `handle_update` and the offset in one
transaction, then `_send_ephemeral`) > `drain_outbox` (`dispatch_events`,
`send_customer_messages`, `send_operator_deliveries`, `send_max_operator_deliveries`,
console queue and `send_operator_console_notifications`) > heartbeat.

MAX inbound (web process): `views.max_webhook` > `max_bot.handle_update` in one
transaction (an operator attachment is downloaded inside it). MAX outbound
(`run_max_bot`): `run` > `iterate` > announce > console queue > `dispatch_events` >
`send_customer_messages` > `send_operator_console_notifications` > purge > heartbeat.

Every outgoing row is claimed as `sending` in a committed transaction before the
network call; recovery turns a leftover `sending` into `uncertain` and never resends.

## Defects confirmed and fixed

1. **Malformed HTTP answers escaped both API clients.** A garbled status line
   (`http.client.BadStatusLine`) or a body cut short (`IncompleteRead`) is
   `http.client.HTTPException`, not `OSError`, so it was not mapped to a network error
   and stopped the worker. It is now a network error, ambiguous for sends.
   Telegram `send_file` also crashed with `AttributeError` on a non-object answer.
2. **MAX sender: one row's unexpected exception stopped the worker** (malformed
   buttons, a defect after the send, an attachment cleanup error). The rest of the
   claimed batch (up to 19 rows) became `uncertain` after the restart and was never
   sent. Now each row has its own boundary.
3. **Telegram senders: the same** for customer messages, operator cards
   (`delivery_content` ran outside any handler) and MAX operator cards.
4. **Console notification senders** (both): an exception after preparation stopped
   the worker. Before the send it is retried by the existing notification policy;
   after the send started it is `uncertain`.
5. **Worker loops** (both) stopped on any exception outside the handled classes,
   including the MAX idle check during a database outage. The loop now records the
   failure, backs off and continues; recovery runs before the next cycle.
6. **Telegram skipped an update on a transient database error** (deadlock, lost
   connection on one statement): the customer's message was dropped and the offset
   moved on. It is now read again, at most three times, then skipped as poisoned.
7. **Failure logs had no location**, and the MAX webhook's database fault was logged
   at warning level, which the production `apps` logger drops.

## Row outcome rules (both workers)

| Where the unexpected error happens | Row outcome | Resent? |
| --- | --- | --- |
| before any send call (bad keyboard, bad data) | `failed`, class in `last_error` | no |
| after a send call started | `uncertain` | never |
| after the row was recorded `sent` | stays `sent`, logged | no |
| database error anywhere | worker database path: `sending` becomes `uncertain` | never |
| `SingleInstanceError`, `BusinessWriteBlocked` | unchanged handling | n/a |

## Logging

Failures log the exception class and its frames (file, line, function) through
`apps.core.observability.exception_trace`, never the exception text: it can carry the
customer's message, a link token or the provider's answer. Worker logs keep the
existing `RedactingFormatter`.

## Residual risks

* A database fault mid-batch still turns every claimed row of that batch into
  `uncertain` (at-most-once by design); none of them is resent.
* A non-database defect affecting every row of a stage repeats each cycle with
  back-off up to 60 s; the worker stays up and reports it in `last_error`.
* The MAX webhook downloads an operator attachment inside its transaction.
* A malformed update the MAX webhook cannot process is acknowledged and dropped
  (no dead-letter table); it is visible only in the error log.
