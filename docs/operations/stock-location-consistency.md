# Согласованность размещения и перемещений

## Источник истины

Текущий физический остаток хранится только в первичных складских сущностях:

- количественный учёт: `StockLot.quantity`, `StockLot.location`, `StockLot.status`;
- поштучный учёт: `PartItem.status`, `PartItem.current_location`;
- активный резерв: `Reservation` и `ReservationLine`.

`StockMovement` является неизменяемым журналом. `StockBalance` является
пересобираемым read-кэшем. `InventoryCountingLine.quantity_counted` и адрес
сессии инвентаризации являются историческим снимком и не должны использоваться
как текущий состав ячейки.

Одна пользовательская операция перемещения хранится в `StockTransfer`.
Документ содержит idempotency token, exact part snapshot, исходную и целевую
ячейки и количество. Если количество распределено по нескольким закупочным
лотам, ledger содержит несколько `StockMovement` с одним `document_id`.

## Read-only диагностика

Локально или в контейнере приложения:

```bash
python manage.py debug_stock_location_consistency
```

Для CI-проверки с ненулевым кодом при расхождениях:

```bash
python manage.py debug_stock_location_consistency --fail-on-issues
```

Команда ничего не изменяет. Она проверяет отрицательные и неразмещённые
физические остатки, дубли размещения, расхождения `StockBalance` с первичкой,
целостность `StockTransfer` относительно ledger и orphan movement rows.

Автоматического repair в этой задаче нет. Любое исправление данных сначала
оформляется отдельной задачей с read-only отчётом и обязательным dry-run.
