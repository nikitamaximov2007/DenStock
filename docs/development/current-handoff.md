# Current handoff

- Task: исправление `/scanner/move/` и согласование текущих остатков.
- Branch: `fix/movement-stock-sync`.
- Base: `a892d606da8796c3d93aefe71221b3ad706ee65c`.
- Worktree: `F:\DenStock-movement-fix`.
- Classification: stock-integrity hotfix with one inventory migration.

## Root cause

1. `resolve_scan(article)` возвращал `PartType`, но `scanner_move` принимал на
   первом шаге только `PartItem`. Поэтому точный артикул `703500875` завершался
   сообщением «Ожидается экземпляр детали», хотя размещённые `StockLot` были.
2. Ручной `move_stock_lot` корректно менял живой `StockLot.location` и обновлял
   `StockBalance`, но карточка первичной инвентаризации показывала исторические
   `InventoryCountingLine.quantity_counted` и `session.full_address` как будто
   это текущий состав ячейки.
3. Страница «Остатки» показывала read-кэш в грани `batch_line + location`, из-за
   чего одна exact-деталь в одной ячейке могла занимать несколько строк.

## Implemented contract

- Source of truth: physical `StockLot` + physical `PartItem`; active reserve is
  read from `ReservationLine`; `StockBalance` remains a rebuildable cache.
- `StockTransfer` is one atomic/idempotent movement document with immutable
  exact identity and location snapshots.
- Bulk movement supports partial quantity and several procurement lots under
  `transaction.atomic` and `select_for_update`.
- Reserved quantity is excluded. Quarantine moves separately and stays in
  quarantine.
- Current cell cards, balances, and the live block in initial inventory use
  live primary stock. Historical inventory rows remain unchanged.
- Read-only command: `debug_stock_location_consistency`.

## Verification

- Targeted movement suite: 55 passed, 0 failed.
- Relevant stock/business suite: 520 passed, 0 failed.
- Full pytest: 1129 passed, 0 failed.
- `ruff check .`: passed.
- `djlint templates --check`: passed after formatting.
- `manage.py check`: passed.
- `makemigrations --check --dry-run`: no changes detected.
- URL reverse: `/scanner/move/` and `/inventory/balance/`.
- Added-line em dash audit and `git diff --check`: passed.

No merge, push, SSH, VPS, production database, deploy, or data repair is part
of this branch.
