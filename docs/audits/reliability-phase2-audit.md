# Reliability Phase 2: warehouse operations under faults, races and stale pages

Baseline: `origin/main` 66debcc (2026-10-04). Candidate branch: `claude/reliability-phase2`.
Executable proof, all reading the database fresh after every scenario:

* `tests/test_reliability_phase2_faults.py`: 111 fault-injection cases (SQLite and PostgreSQL),
  lot and serial-item paths.
* `tests/test_reliability_phase2.py`: expected conflicts, duplicate submits, stale pages.
* `tests/test_reliability_phase2_postgresql.py`: 23 real PostgreSQL 16 races, with the
  interleaving forced at the exact lock statement (`connection.execute_wrapper`).

## Lock order (the rule after this phase)

Document header, then its lines, then stock rows (serial items, then lots, each in
primary-key order, via `inventory.services.lock_stock_sources`), then storage cells
(`ensure_location_operation_allowed`). Consuming writers always reached cells after
their lot; transfer and manual reservation activation reached the cell first and
deadlocked against them. Section recount apply already follows the rule (batch lines,
lots, then cells); recount start locks cells only.

## Defects confirmed and fixed

| # | Defect | Observed on main | Fix |
|---|---|---|---|
| 1 | Two documents completing the same lots in opposite line order (sale/sale, sale/repair, sale/write-off) | PostgreSQL `deadlock detected`, HTTP 500 | `lock_stock_sources` before per-line work |
| 2 | Stock transfer and manual reservation activation lock the cell before the lot; a sale locks the lot, then the cell | deadlock | lots first, cells after |
| 3 | Partial sale-line cancellation locked the line, then the sale; whole-sale cancellation the reverse | deadlock | sale first (as repairs already did) |
| 4 | A return drafted while its sale or repair is being cancelled can be completed afterwards | stock restored twice (lot 7 after selling 2 of 5) | `complete_return` locks the source document and requires it completed |
| 5 | Receipt draft edits (add, edit, remove line, header) were not atomic and trusted the page's copy | posted receipt with an unreceived line; posted line 9 while 3 were received | atomic edit, receipt locked and re-read |
| 6 | Quick Actions cart edits trusted the page's copy | completed sale left with no lines while its stock was consumed; a stale "discard" deleted a completed sale | document locked and re-read before any row change |
| 7 | Quick write-off had no duplicate-submit guard | two write-offs for one form | one-time `request_token` (new nullable unique field) |
| 8 | Partial sale/repair line cancellation repeated by a second submit | two cancellations | the form carries the remaining quantity it showed; a change is refused |
| 9 | A cell under recount during sale/repair/write-off completion, reservation activation or a Quick Action | HTTP 500 (`InventoryError` escaped) | mapped to the operation's own error |
| 10 | Cancelling a Quick Action sale while a return draft is open | HTTP 500 (`SaleError` escaped) | mapped to `ActionError` |
| 11 | Found-stock group posting (scanner receiving) locked the cell before the lots it adds to | deadlock against a sale in the same cell; after fix 2 also against a transfer | candidate lots first, then the cell |

No `except Exception` was added. Unexpected defects still surface as errors.

## What held on main already

Every operation's own transaction boundary: 102 of the 111 fault cases pass on main
unchanged (the 9 others need the new duplicate guards of rows 7 and 8). Repeated
completion and cancellation are idempotent or refused. Stale inventory counts are
refused. Reservation versus reservation, reservation removal versus conversion,
inventory count versus sale or repair, and Quick Action cancellation versus sale
cancellation serialize correctly.

## Residual risks

* Lots inside one Quick Actions cart row and one Quick Action are locked in FIFO
  (`created_at`, `pk`) order; it equals key order unless lots were created out of
  sequence.
* Cancellation and return paths restore lots line by line; two cancellations sharing
  lots in opposite order were not reproduced and were not changed.
* A database deadlock or lost connection outside the covered pairs is still a server
  error for that request; nothing is committed.
* Adding the same draft line twice (duplicate POST on a draft) adds two draft lines;
  nothing moves until completion, which re-validates stock.
