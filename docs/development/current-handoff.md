# Current handoff

Нет активной передачи: задача завершена.

- Task: Fix warehouse action identity (номер детали подменялся) + add sale
  cancellation (DONE)
- Branch: fix/warehouse-action-identity-cancel
- Root cause: `PartNumber.Meta.ordering = ["kind", "value"]`, где `analog`
  сортировался раньше `oem`; старый отчёт через `part_type.numbers.first`
  мог показать замену 420931284 вместо primary/OEM 420931285. Таможенный блок
  также мог брать номер из BRP link/source, а не из фактически проданного
  номера.
- Completed:
  - `WarehouseAction` хранит snapshot identity: `part_number`, `part_name`,
    `location_code`, `price_source_number`, а также статус и поля отмены.
  - `perform_action` заполняет snapshot exact scanned/sold номером; источник
    цены хранится отдельно и не подменяет номер действия.
  - Отчёт, customs readiness и Excel используют snapshot `part_number`;
    отменённые действия исключены из итогов, customs readiness и Excel, но
    доступны для аудита через фильтр.
  - Добавлена безопасная отмена продажи через UI и management command:
    возврат остатка через `inventory.return_stock_lot_quantity`, `Sale` ->
    `VOIDED`, `WarehouseAction` -> `CANCELLED`.
  - Добавлена диагностика `debug_warehouse_actions --material-no`.
  - Миграция backfill заполняет snapshot существующих действий primary номером
    детали (`-is_primary`, `pk`), названием и кодом ячейки.
- Checks run:
  - `python -m pytest tests/test_actions.py` (35 passed)
  - `python manage.py check`
  - `python manage.py makemigrations --check`
  - `python -m ruff check .`
  - `python -m djlint templates --check`
  - `python -m pytest` (858 passed)
- Local note: `makemigrations --check` прошёл с `No changes detected`, но
  локально предупредил, что host `db` из `DATABASE_URL` недоступен; это не
  блокирует проверку миграций.
