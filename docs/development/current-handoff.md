# Current handoff

- Task: согласовать общую оценку склада по цене продажи с категориями.
- Branch: `fix/finance-category-valuation`.
- Base: `627a84bc896e3d6ff7f07d4eb561aef683bf2123`.
- Worktree: `F:\DenStock-finance-stats-fix`.
- Classification: read-only finance statistics hotfix, no migration.

## Root cause

Верхняя карточка использовала физические `StockLot`/`PartItem` и текущую
клиентскую цену BRP/Polaris. Таблица категорий отдельно считала только
`AVAILABLE` по landed cost. В regression-сценарии одна категория BRP давала
`3.00` в таблице и `4410.00` в общей карточке.

## Implemented contract

- `get_warehouse_valuation()` один раз собирает physical quantity и текущую
  клиентскую цену exact-карточки.
- BRP/Polaris pricing services, текущие курс и наценки сохранены; manual price
  читается из `PartType.recommended_price`.
- Категории группируются по `Category.pk`, итог карточки складывается из этих
  же округлённых строк.
- Резерв и карантин входят как physical; продажа и списание не входят.
- Позиции без клиентской цены остаются в количестве категории и показываются
  отдельным счётчиком.
- UI показывает количество и строку «Итого по категориям».

## Verification

- Targeted statistics and finance: 45 passed, 0 failed.
- Relevant pricing and stock suite: 421 passed, 0 failed.
- Full pytest: 1216 passed, 0 failed.
- `ruff check .`: passed.
- `djlint templates --check`: passed, 95 files checked.
- `manage.py check`: passed.
- `makemigrations --check --dry-run`: no changes detected.
- `git diff --check`: passed.
- Replacement/superseded query regression: at most 8 queries for ten linked
  BRP/Polaris positions with zero exact prices.

Merge, push, SSH, VPS, production database and deploy are outside this task.
