# План реализации — Слой 18. Возвраты на склад (физическое обратное поступление)

**Статус:** УТВЕРЖДЁН (2026-06-26) · все рекомендации приняты · реализация в границах.

---

## 1. Цель слоя

Сделать **контролируемый возврат детали обратно на склад** после продажи (Слой 16)
или выдачи в ремонт (Слой 17). Это **физическое обратное поступление**:

- возвращает **физический остаток** (`PartItem` снова на складе / `StockLot.quantity`
  растёт);
- создаёт **`StockMovement` возврата** (`RETURN_ITEM`/`RETURN_LOT`);
- **фиксирует себестоимость** возврата из исходной строки (не пересчитывает по
  текущему landed cost);
- связывает возврат с исходной `SaleLine`/`RepairIssueLine` и **не даёт вернуть
  больше**, чем было продано/выдано (минус уже возвращённое).

**Главная мысль (граница слоя):** Слой 18 — это **возврат на склад**, а НЕ
**возврат денег**. Здесь нет кассового refund, чека, гарантии, финансового сторно
продажи и пересчёта прибыли. Два мира — «деталь обратно на полку» и «деньги
обратно клиенту» — намеренно не смешиваются; в этом слое только первый.

**Главный архитектурный контроль (как в Слоях 10/12/14/15/16/17):** физическое
поступление и запись ledger идут **только через сервисы `apps/inventory`** (новые
`return_part_item` / `return_stock_lot_quantity`). **Документ возврата ведёт
`apps/returns` сам, но `StockMovement`/`StockBalance`/`PartItem.status`/
`StockLot.quantity` напрямую НЕ трогает.** Граница закрепляется тестом-моком (§22).

### Что уже есть (переиспользуем)

| Уже реализовано | Где | Роль в Слое 18 |
|---|---|---|
| `SaleLine` (part_item/stock_lot, quantity, unit_cost_rub) | `apps/sales` (Слой 16) | источник возврата проданного |
| `RepairIssueLine` (part_item/stock_lot, quantity, unit_cost_rub) | `apps/repairs` (Слой 17) | источник возврата выданного в ремонт |
| `PartItem.Status.{SOLD,INSTALLED}` + `QUARANTINE`/`AVAILABLE` | `inventory/models.py` | исходный и целевой статус возвращаемого экземпляра |
| `StockLot` + `UniqueConstraint(batch_line, location)`; `DEPLETED` | `inventory` | возврат количества: оживление/находка лота в ячейке (§8) |
| `StockMovement.document_type/document_id` + `_record_movement(...)` | `inventory` | связь движения с `StockReturn` |
| `_refresh_balance`, `recompute_balance_row`, `ITEM/LOT_PHYSICAL_STATUSES` | `inventory/services.py` | баланс пересобирается из первички автоматически |
| `StorageLocation.can_hold_stock()` (= is_active ∧ storage_allowed) | `warehouse` | валидация ячейки возврата |
| `NumberSequence`, `money()`, `can_view_purchase_cost` | `inventory`/`procurement`/`accounts` | номер `RET-`, округление, скрытие себестоимости |

### Что нового в Слое 18

- Новое приложение **`apps/returns`**: модели `StockReturn`, `StockReturnLine`,
  сервисы, вьюхи, шаблоны.
- Новые типы движения **`RETURN_ITEM` / `RETURN_LOT`** (`inventory.MovementType`) →
  **миграция инвентаря** `0007` (`AlterField` choices).
- Сервисы `return_part_item` / `return_stock_lot_quantity` в `apps/inventory`
  (физика + ledger, источник-агностичны).
- Возможность **`MANAGE_RETURNS`**, экраны возвратов, действие «Оформить возврат»
  из карточек `Sale` и `RepairOrder`.

**Чего слой НЕ делает:** денежный refund, касса, чеки, гарантийный модуль, сторно
продажи, изменение финансовых итогов `Sale`/прибыли задним числом, изменение
статуса `completed` у `Sale`/`RepairOrder`, CRM, аналитика, PDF; не превращает
`StockBalance` в источник истины; не пишет `StockMovement` из вьюх.

---

## 2. Какие типы возвратов — **рекомендация: все 4 сразу**

| # | Сценарий | Источник | Что меняется |
|---|---|---|---|
| 1 | Возврат проданного `PartItem` | `SaleLine` (part_item) | `sold → quarantine/available`, physical↑ |
| 2 | Возврат количества из проданного `StockLot` | `SaleLine` (stock_lot) | лот.quantity↑ в ячейке возврата |
| 3 | Возврат выданного в ремонт `PartItem` | `RepairIssueLine` (part_item) | `installed → quarantine/available`, physical↑ |
| 4 | Возврат количества из выданного `StockLot` | `RepairIssueLine` (stock_lot) | лот.quantity↑ в ячейке возврата |

**Обоснование «все 4».** Физика возврата **одинакова** для всех четырёх:
экземпляр возвращается на полку, либо количество добавляется в лот ячейки.
Различается только **тип строки-источника** (`SaleLine` vs `RepairIssueLine`),
который в `apps/returns` обрабатывается полиморфно (XOR двух FK). Сервисы инвентаря
(`return_part_item`/`return_stock_lot_quantity`) **источник-агностичны** — им всё
равно, откуда пришёл объект. Поэтому поддержать оба источника стоит почти столько
же, сколько один, и слой остаётся симметричным (продажа+ремонт → возврат обоих).
Урезать до «только продажи» создало бы асимметрию и второй слой возвратов позже.

---

## 3. Где размещаем домен — **рекомендация: отдельное `apps/returns`**

| Вариант | За | Против | Вывод |
|---|---|---|---|
| **`apps/returns` (рекомендация)** | возврат ссылается и на `SaleLine`, и на `RepairIssueLine` — нейтральный третий модуль; «возврат на склад» явно отделён от «возврата денег»; повторяет паттерн «документ = своё приложение» (sales/repairs/returns) | новое приложение | **выбираем** |
| `apps/sales` | уже есть | возврат ремонта тянул бы `sales → repairs` (или дублирование); смешало бы склад и будущий денежный refund | нет |
| `apps/repairs` | уже есть | то же зеркально (`repairs → sales`) | нет |
| `apps/inventory` (минимально) | физика и так там | бизнес-документ возврата (источник/причина/статус) — не дело ledger-слоя; раздуло бы inventory | только **физические сервисы** (§14) |

**Обоснование.** `apps/returns` зависит от **обоих** доменов и от inventory:
`returns → {inventory, sales, repairs}` — **ацикл��чно** (ни sales, ни repairs, ни
inventory не импортируют returns). Физические сервисы (источник-агностичные) живут
в `inventory`; бизнес-документ — в `returns`. Так возврат **на склад** не
смешивается с будущим возвратом **денег** (он будет отдельным finance/cash-слоем).

---

## 4. Сущности

Две модели в `apps/returns/models.py`. **Без CRM и гарантийного модуля.**

### 4.1 `StockReturn` (шапка возврата)

Возврат оформляется **из одного документа-источника** (одна `Sale` ИЛИ один
`RepairOrder`): `source_type` ∈ {`sale`, `repair_order`}, `source_id` — id этого
документа (лёгкий указатель, как `StockMovement.document_*`, без contenttypes).

### 4.2 `StockReturnLine` (строка возврата)

Каждая строка ссылается **ровно на одну** исходную строку: `source_sale_line` XOR
`source_repair_line`. Денормализует `part_item`/`stock_lot` из источника, хранит
**ячейку возврата** и **целевое состояние** (карантин/доступен), замораживает
себестоимость.

---

## 5. Поля `StockReturn`

| Поле | Тип | Назначение |
|---|---|---|
| `number` | `CharField` unique, editable=False (`RET-000001`) | номер возврата |
| `status` | `CharField(choices=Status)` | `draft`/`completed` (§6) |
| `source_type` | `CharField(choices)` `"sale"`/`"repair_order"` | тип документа-источника |
| `source_id` | `PositiveIntegerField` | id `Sale`/`RepairOrder` |
| `reason` | `CharField` blank | причина возврата (текст) |
| `comment` | `CharField` blank | примечание |
| `cost_total` | `Decimal(14,2)` default 0 | сумма себестоимости возвращённого (заморожена) |
| `created_by` | FK user, `SET_NULL` | кто создал |
| `created_at` / `updated_at` | auto | аудит |
| `completed_at` | `DateTimeField` null/blank | момент проведения |

Номер `RET-000001` — ASCII, коротко и однозначно («возврат»), единообразно с
`S-`/`R-`. Отдельный ключ `NumberSequence "stock_return"` (seed-миграция, prefix
`RET-`).

---

## 6. Поля `StockReturnLine`

| Поле | Тип | Назначение |
|---|---|---|
| `stock_return` | FK `StockReturn`, `CASCADE`, related_name="lines" | шапка |
| `source_sale_line` | FK `sales.SaleLine`, `PROTECT`, null/blank | источник (XOR) |
| `source_repair_line` | FK `repairs.RepairIssueLine`, `PROTECT`, null/blank | источник (XOR) |
| `part_type` | FK `catalog.PartType`, `PROTECT` | денормализация |
| `part_item` | FK `inventory.PartItem`, `PROTECT`, null | поштучно (из источника) |
| `stock_lot` | FK `inventory.StockLot`, `PROTECT`, null | лот-источник (для трассировки) |
| `batch` / `batch_line` | FK `procurement.*`, `PROTECT` | денормализация себестоимости/ячейки |
| `quantity` | `Decimal(12,3)` | 1 для экземпляра; ≤ возвращаемого для лота |
| `to_location` | FK `warehouse.StorageLocation`, `PROTECT` | **ячейка возврата** |
| `restock_status` | `CharField(choices)` `available`/`quarantine` | целевое состояние (§7) |
| `unit_cost_rub` | `Decimal(12,2)` editable=False | себестоимость за ед., **заморожена из источника** |
| `total_cost_rub` | `Decimal(14,2)` editable=False | `money(unit_cost × quantity)` |
| `returned_lot` | FK `inventory.StockLot`, `PROTECT`, null | лот, в который фактически зачислено (для лотов; заполняется при проведении) |
| `created_at` | auto | аудит |

**Ограничения БД:**
- `CheckConstraint` XOR(`source_sale_line`, `source_repair_line`);
- `CheckConstraint` XOR(`part_item`, `stock_lot`);
- `CheckConstraint` `quantity > 0`.

---

## 7. Возврат `PartItem` — целевой статус

- Экземпляр из **`sold`** (продажа) или **`installed`** (ремонт) возвращается на
  склад: задаётся **ячейка возврата** (`to_location`) и **целевое состояние**
  (`restock_status`).
- **Правило (рекомендация):**
  - **по умолчанию `quarantine`** — возвращённая деталь может быть б/у,
    повреждённой или требовать проверки; карантин держит её физически на складе,
    но **вне `available`** (продать/выдать снова можно только после явного снятия
    карантина — это существующий переход `quarantine → available` Слоя 8);
  - **`available`** разрешаем только **явным выбором** пользователя.
- `current_location` экземпляра **устанавливается в `to_location`** (раньше там
  была последняя складская ячейка как аудит-след; теперь деталь снова там физически).

**Почему НЕ статус `returned`.** В enum есть `PartItem.Status.RETURNED`, но он **не
входит** в `ITEM_PHYSICAL_STATUSES` → постановка в `returned` **не восстановила бы**
физический остаток (а цель слоя — восстановить). Поэтому возвращаем в **физические**
`quarantine`/`available`. Значение `returned` остаётся заделом под иной будущий
сценарий (например, возврат поставщику/списание-возврат), здесь не используется.

---

## 8. Возврат количества из `StockLot` — безопасное решение под `UniqueConstraint`

`StockLot` уникален по **(`batch_line`, `location`)** (`uniq_stocklot_line_location`)
— в одной ячейке не может быть двух лотов одной строки партии. Поэтому возврат
количества **не создаёт всегда новый лот**, а действует так (в `return_stock_lot_quantity`):

1. Берём `batch_line` исходного лота и выбранную `to_location`.
2. Ищем лот в **(`batch_line`, `to_location`)** под `select_for_update`:
   - **нет лота** → создаём **новый** лот: `quantity = возврат`,
     `initial_quantity = возврат`, `status = restock_status`,
     `landed_unit_cost_rub = unit_cost из источника`;
   - **есть лот в статусе `depleted`** → **оживляем**: `quantity += возврат`,
     `status = restock_status` (типичный случай — вернули в ту же ячейку, где лот
     обнулился продажей/выдачей);
   - **есть лот в физическом статусе, совпадающем с `restock_status`** →
     `quantity += возврат` (доливаем);
   - **есть лот в физическом статусе, отличном от `restock_status`** → **ошибка**
     `InventoryError`: «В ячейке уже есть лот этой строки в другом статусе; выберите
     другую ячейку или согласуйте статус» (слияние разных статусов в один лот —
     осознанно не делаем, как и слияние лотов в `move_stock_lot`).
3. Записываем `RETURN_LOT` (`to_location`, qty, document=stock_return), пересобираем
   баланс. В строке возврата сохраняем `returned_lot` (куда зачислено).

**Обоснование.** Это снимает конфликт уникальности и даёт предсказуемое поведение:
«вернули туда, где лот был» — оживляем; «вернули в новую ячейку» — создаём лот;
«ячейка занята лотом другого статуса» — явная ошибка, без скрытого смешивания
карантина и доступного в одном лоте. Лот **не дробим**; новый лот появляется только
когда в целевой ячейке лота этой строки нет.

---

## 9. `StockMovement`

| Поле движения | Значение |
|---|---|
| `movement_type` | **`RETURN_ITEM`** / **`RETURN_LOT`** |
| `from_location` | **`null`** (поступление извне склада) |
| `to_location` | ячейка возврата (`to_location`) |
| `quantity` | возвращаемое количество (1 / qty) |
| `unit_cost_rub` | себестоимость из исходной `SaleLine`/`RepairIssueLine` (заморожена) |
| `total_cost_rub` | `unit_cost × quantity` (в `StockMovement.save()`) |
| `document_type` | **`"stock_return"`** |
| `document_id` | `StockReturn.id` |

**Название `RETURN_ITEM`/`RETURN_LOT` (универсальный возврат на склад,
рекомендация).** Обоснование: движение описывает **физический факт** «деталь
вернулась на склад» — он одинаков для возврата продажи и возврата ремонта. Откуда
именно (продажа/ремонт) — это **бизнес-контекст документа** (`document_type=
"stock_return"`, а сам `StockReturn` хранит `source_type`), а не свойство движения.
Так не плодим `SALE_RETURN_*`/`REPAIR_RETURN_*` (зеркально решению `ISSUE_*` Слоя
17). Альтернатива с раздельными типами привязала бы движение к источнику без выгоды
(в открытых вопросах).

**Минимальная правка инвентаря:** `MovementType += RETURN_ITEM, RETURN_LOT` →
миграция `inventory/0007_alter_stockmovement_movement_type.py` (только `AlterField`).

---

## 10. `StockBalance`

- Остаётся **кэшем** (не источник истины).
- `quantity_physical` **увеличивается** (экземпляр снова в физическом статусе /
  `lot.quantity` вырос) через `inventory.return_*` → `_refresh_balance`.
- `available`/`quarantine` зависят от `restock_status`: возврат в `quarantine`
  поднимает `physical` и `quarantine` (а `available = physical − quarantine −
  reserved` не растёт); возврат в `available` поднимает `physical` и `available`.
- `reserved` **не меняется**.
- **Важно:** баланс считается из **первички** (`StockLot.quantity` + `PartItem.status`),
  а не из суммы движений. Поэтому возврат «доезжает» в кэш автоматически, как только
  `return_*` обновил первичный объект и вызвал `_refresh_balance`. Движения
  `RETURN_*` — это **журнал/аудит**; `rebuild_stock_balance` (полная пересборка из
  первички) и `check_stock_balance` (сверка) продолжают работать без правок.

---

## 11. Источник возврата и инвариант «не больше проданного/выданного»

- Каждая `StockReturnLine` ссылается на **одну** исходную строку
  (`source_sale_line` XOR `source_repair_line`).
- **Возвращаемое к данному моменту**:
  `returnable = source_line.quantity − Σ(уже завершённые возвраты этой строки)
  − Σ(эта же строка в текущем черновике возврата)`.
- Проверяется **дважды**: при добавлении строки (черновик) и **повторно** при
  проведении (под блокировкой). Если за это время возвращаемое исчерпано — ошибка.
- Хелпер `returned_quantity_for(source_line) -> Decimal` суммирует количество по
  **завершённым** `StockReturnLine` данной исходной строки (по `source_sale_line`/
  `source_repair_line` со статусом шапки `completed`).
- Для `PartItem`-источника (`quantity = 1`): после одного возврата `returnable = 0`.

---

## 12. Себестоимость

- `unit_cost_rub` строки возврата **замораживается из исходной строки**
  (`SaleLine.unit_cost_rub` / `RepairIssueLine.unit_cost_rub`), **не** пересчитывается
  по текущему landed cost; `total_cost_rub = money(unit_cost × quantity)`.
- `StockReturn.cost_total` — сумма строк, заморожена при проведении.
- Так **история сходится**: вернули по той же себестоимости, по которой списали;
  будущие изменения landed cost историю возврата не двигают (как в продажах/ремонте).
- Себестоимость видна **только при `can_view_purchase_cost`** (контекст `show_costs`).

> Примечание о себестоимости физического движения: для `PartItem`
> `item.landed_cost_rub` неизменна и равна `SaleLine/RepairIssueLine.unit_cost_rub`,
> поэтому `_record_movement` даёт ту же сумму. Для нового/оживлённого лота
> `landed_unit_cost_rub` берётся из источника (новый лот) либо уже задан (оживление).

---

## 13. Деньги — НЕТ

На этом слое **нет** денежного refund, **нет** кассы, **нет** чека, **нет**
финансового сторно; **прибыль продажи не пересчитываем**, итоги `Sale` не трогаем.

**Как не сломать отчётность будущих слоёв.** Возврат — **отдельный ledger**
(`StockReturn` + движения `RETURN_*`), он ничего не вычитает из `Sale`/`SaleLine`
задним числом. Будущий финансовый слой сможет **нетто-зачесть** возвраты против
продаж по связи `StockReturnLine.source_sale_line` и движениям `RETURN_*`, не
страдая от «переписанной истории». Принцип тот же, что в Слоях 16–17: документы
неизменяемы, корректировки — отдельными документами.

---

## 14. Сервисы `apps/inventory` (источник-агностичные)

```python
@transaction.atomic
def return_part_item(item, to_location, *, restock_status, by=None,
                     document_id=None, comment="") -> PartItem:
    # lock item; status ∈ {SOLD, INSTALLED} (иначе InventoryError);
    # restock_status ∈ {AVAILABLE, QUARANTINE}; to_location.can_hold_stock();
    # item.status = restock_status; item.current_location = to_location;
    # _record_movement(RETURN_ITEM, from=None, to=to_location, document_type="stock_return");
    # _refresh_balance.

@transaction.atomic
def return_stock_lot_quantity(batch_line, to_location, quantity, *, unit_cost_rub,
                              restock_status, batch=None, by=None,
                              document_id=None, comment="") -> StockLot:
    # quantity > 0; restock_status ∈ {AVAILABLE, QUARANTINE}; to_location.can_hold_stock();
    # найти/создать/оживить лот в (batch_line, to_location) по правилу §8;
    # lot.quantity += quantity; status = restock_status;
    # _record_movement(RETURN_LOT, from=None, to=to_location, document_type="stock_return");
    # _refresh_balance; вернуть лот.
```

- В `transaction.atomic`, под `select_for_update` (item / целевой лот строки).
- Меняют **физику**, создают `RETURN_*`, обновляют `StockBalance`.
- **Не знают** о `Sale`/`RepairOrder`/`StockReturn` — берут готовые `to_location`,
  `restock_status`, `unit_cost_rub` (резерв/источник проверяет `apps/returns`).
- Это **инверс** `_consume_*` Слоя 16/17; общий helper с ними не делаем (разные
  направления, разные инварианты) — отдельные функции читаемее (открытый вопрос).

---

## 15. Сервисы `apps/returns/services.py`

```python
class ReturnError(Exception): ...

create_return(*, source, reason="", comment="", by) -> StockReturn  # source=Sale|RepairOrder
add_sale_line_return(ret, sale_line, quantity, *, to_location, restock_status, by) -> StockReturnLine
add_repair_line_return(ret, repair_line, quantity, *, to_location, restock_status, by) -> StockReturnLine
remove_return_line(line, *, by)
complete_return(ret, *, by) -> StockReturn          # draft → completed (физический возврат)
returned_quantity_for(source_line) -> Decimal       # сумма завершённых возвратов строки
calculate_return_costs(ret) -> Decimal              # сумма из замороженных строк
```

**`complete_return` (оркестрация, `@transaction.atomic`):**
1. lock возврат; должен быть `draft`; строк ≥ 1 (иначе `ReturnError`).
2. по каждой строке (под `select_for_update` объекта-источника):
   - повторная проверка `returnable` (§11) и валидность `to_location`/`restock_status`;
   - **заморозка себестоимости** из исходной строки (`unit_cost_rub`/`total_cost_rub`);
   - физический возврат:
     - `part_item` → `inventory.return_part_item(item, to_location, restock_status=…,
       document_id=ret.pk)`;
     - `stock_lot` → `inventory.return_stock_lot_quantity(batch_line, to_location,
       qty, unit_cost_rub=…, restock_status=…, batch=…, document_id=ret.pk)`;
       результат пишем в `line.returned_lot`.
3. `cost_total` = сумма строк; `status = completed`, `completed_at = now`.

- Все действия — **только через сервисы**; вьюхи ledger не пишут (тест §22).
- `create_return` принимает `Sale` или `RepairOrder`, проставляет `source_type`/
  `source_id`. `add_*` валидируют, что строка-источник принадлежит этому документу
  и что объект подлежит возврату (продан/выдан).

---

## 16. Инварианты (и кто гарантирует)

| Инвариант | Гарант |
|---|---|
| Нельзя провести пустой возврат | `complete_return`: строк ≥ 1 |
| Нельзя вернуть больше проданного/выданного (минус уже возвращённое) | `returnable` (§11), add + complete |
| Нельзя вернуть `PartItem`, уже доступный на складе | `return_part_item`: статус ∈ {sold, installed} |
| Нельзя вернуть `quantity ≤ 0` | сервисы: `quantity > 0` |
| Нельзя вернуть в `inactive`/`storage_allowed=false` | сервисы: `to_location.can_hold_stock()` |
| Нельзя провести один возврат дважды | `complete_return` требует `draft` |
| Проведённый возврат **immutable** | сервисы: мутации только при `draft` |
| Возврат **создаёт** `StockMovement` (`RETURN_*`) | `inventory.return_*` |
| Возврат **увеличивает** physical (`status`/`quantity`) | `inventory.return_*` |
| Возврат **фиксирует** себестоимость из источника | `complete_return` (заморозка строки) |
| Возврат **не** создаёт `Sale` | границы (нет создания продаж) |
| Возврат **не** создаёт оплату/чек/денежный refund | границы (нет платёжных полей/логики) |

---

## 17. Транзакции и блокировки

- `complete_return` — целиком в `transaction.atomic`.
- `select_for_update` на `StockReturn`, и в `return_*` — на `PartItem`/целевом
  `StockLot` (находка/оживление лота тоже под блокировкой строки в (`batch_line`,
  `to_location`)).
- **Защита от двойного возврата:** `complete_return` требует `draft`; `returnable`
  пересчитывается под блокировкой источника.
- Последовательные тесты на невозможность вернуть больше доступного к возврату;
  конкурентный Postgres-тест — будущий слой (тестовый стек SQLite).

---

## 18. Права

Новая возможность **`MANAGE_RETURNS`** (`roles.py`):

| Роль | Проводить возврат | Видеть возвраты | Себестоимость |
|---|---|---|---|
| Администратор | ✅ | ✅ | ✅ |
| Руководитель | ✅ | ✅ | ✅ |
| **Кладовщик** | ✅ | ✅ | нет |
| Продавец/Мастер | ❌ | ✅ | нет |
| Наблюдатель | ❌ | ✅ | по `can_view_purchase_cost` |

**Обоснование.** Возврат **на склад** — это прежде всего **складское приёмное**
действие (физически положить деталь на полку), поэтому право даём **кладовщику**
(а также Админу/Руководителю). **Продавцу/Мастеру право проводить НЕ даём**: иначе
возврат стал бы скрытым каналом «отмены продажи» в обход финансового контроля —
продавец может **видеть** возвраты, но не проводить их. Наблюдатель — только
просмотр. Себестоимость — под `can_view_purchase_cost`.

- `roles.py`: `MANAGE_RETURNS` для `ADMIN`/`MANAGER`/`STOREKEEPER`.
- `accounts/models.py`: `can_manage_returns`. Без миграции (возможности — код).
- Просмотр — `login_required`; мутации — под `manage_returns`.

---

## 19. UI (`apps/returns`, шаблоны `templates/returns/`)

| Экран | URL (`name`) | Право |
|---|---|---|
| Список возвратов | `/returns/` (`return_list`) | просмотр — вошедшие |
| Карточка возврата | `…/<pk>/` (`return_detail`) | просмотр — вошедшие |
| Создать (из Sale/Repair) | `…/new/?source=sale&id=…` (`return_create`) | `manage_returns` |
| Добавить строку из источника | POST `…/<pk>/add-line/` | `manage_returns` |
| Снять строку | POST `…/lines/<pk>/remove/` | `manage_returns` |
| Провести возврат | POST `…/<pk>/complete/` | `manage_returns` |

- Создание: выбор **источника** (`sale`/`repair`) и документа; затем добавление
  строк из его `SaleLine`/`RepairIssueLine` с **выбором ячейки возврата** и
  **состояния** (`quarantine`/`available`, по умолчанию карантин).
- Карточка: источник (ссылка на `Sale`/`RepairOrder`), строки (деталь,
  экземпляр/лот, кол-во, ячейка, состояние), **движения `RETURN_*`**;
  **себестоимость строк и `cost_total` — только `can_view_purchase_cost`**.
- Проведённый возврат — без кнопок правки (immutable).
- No-JS: server-rendered формы; hidden/query-поля недоверенные (§22).

---

## 20. Интеграция

- Ссылка **«Оформить возврат»** из карточки `Sale` (`templates/sales/sale_detail.html`)
  — для `manage_returns`, ведёт на `return_create?source=sale&id=<sale.pk>`.
- Ссылка **«Оформить возврат»** из карточки `RepairOrder`
  (`templates/repairs/repair_order_detail.html`) — аналогично `source=repair`.
- После проведения возврата `/search/` показывает **увеличенный остаток** (кэш
  пересобран сервисом).
- **Не добавляем** быстрый checkout/однокликовый возврат.

---

## 21. Влияние на `Sale` и `RepairOrder`

- На Слое 18 **не меняем** статус `completed` у `Sale`/`RepairOrder`.
- **Не делаем** финансовый reversal и не трогаем `revenue_total`/`profit_total`/
  `cost_total` продажи/заказа.
- На карточке `Sale`/`RepairOrder` можно показать **«возвращено N»** по связанным
  `StockReturnLine` (read-only агрегат), чтобы было видно остаток к возврату.
- Полное сторно/финансовая корректировка — **будущий слой**.

---

## 22. Тесты (`tests/test_returns.py`)

1. Можно создать черновик возврата (`draft`).
2. Нельзя провести пустой возврат (`ReturnError`).
3. Можно вернуть проданный `PartItem` из `SaleLine`.
4. `PartItem` возвращается в `quarantine` (по умолчанию) / `available` (явно).
5. Создаётся `StockMovement` `RETURN_ITEM` (`from=null`, `to=ячейка`,
   `document_type="stock_return"`, `document_id=ret.id`, qty=1).
6. `StockBalance` увеличивается (physical↑; available/quarantine по состоянию).
7. Можно вернуть выданный в ремонт `installed` `PartItem` из `RepairIssueLine`.
8. Можно вернуть количество из проданного `StockLot` (`SaleLine`) → лот.quantity↑.
9. Оживление `depleted`-лота при возврате в ту же ячейку (status → restock).
10. Можно вернуть количество из выданного `StockLot` (`RepairIssueLine`).
11. Новый лот при возврате в ячейку без лота этой строки; конфликт статуса в
    занятой ячейке → ошибка.
12. Нельзя вернуть больше, чем продано/выдано (с учётом уже возвращённого).
13. Нельзя провести возврат дважды (`completed` immutable).
14. Нельзя вернуть в `storage_allowed=false` ячейку.
15. Нельзя вернуть в `inactive` ячейку.
16. `RepairIssueLine`/`SaleLine` фиксируют себестоимость возврата (`unit_cost_rub`/
    `total_cost_rub` из источника, не из текущего landed).
17. **Архитектурный мок:** при проведении вьюха вызывает сервис и сама
    `StockMovement`/`StockBalance` не пишет (`patch` сервиса → ledger неизменен).
18. Hidden/query-параметры перепроверяются сервером (подмена возврата/строки/
    ячейки/qty → ошибка/404, без эффекта).
19. **Возврат не создаёт `Sale`**.
20. **Возврат не создаёт оплату/чек/refund** (в модели нет платёжных полей).
21. Пользователь с `manage_returns` (Кладовщик) может провести.
22. Пользователь без права (Продавец/Мастер) — **403** на проведение (но видит список).
23. Себестоимость скрыта без `can_view_purchase_cost`.
24. Регресс: `Sale`/`RepairOrder` после возврата **остаются `completed`**, их
    финансовые итоги не изменены.

---

## 23. Management-команды

- **Не требуются.** Себестоимость замораживается при проведении; баланс
  пересобирается существующими `rebuild_stock_balance`/`check_stock_balance` из
  первички (возврат уже отражён в `StockLot.quantity`/`PartItem.status`).
- Опционально (только при необходимости) — `check_returned_quantity` для сверки
  «Σ возвратов ≤ Σ продаж/выдач» по строкам-источникам. По умолчанию **нет**;
  ввести лишь если потребуется аудит целостности.

---

## 24. Ручная проверка

1. Кладовщиком открыть проведённую `Sale` → «Оформить возврат» → создать черновик.
2. Добавить строку из `SaleLine` (экземпляр) с ячейкой и состоянием `quarantine` →
   провести. В «Движениях» — `RETURN_ITEM` (`—`→ячейка, `document=stock_return`);
   в `/search/` «физически» выросло, «доступно» не выросло (карантин); экземпляр —
   `quarantine`.
3. Повторить с `available` → «доступно» выросло.
4. Открыть проведённый `RepairOrder` → «Оформить возврат» → вернуть `installed`
   экземпляр и/или количество из лота. Лот.quantity вырос (или ожил из `depleted`).
5. Попробовать вернуть больше, чем было продано/выдано → отказ.
6. Попробовать провести тот же возврат повторно → отказ (immutable).
7. Попробовать вернуть в неактивную/`storage_allowed=false` ячейку → отказ.
8. Себестоимость строк/`cost_total`: кладовщику не видна; админу — видна.
9. Продавцом/Мастером → проведение недоступно (403), список виден.
10. Убедиться: `Sale`/`RepairOrder` остались `completed`, их суммы не изменились.

---

## 25. Критерии готовности

1. Возврат `PartItem`/`StockLot` идёт **только через сервисы**: документ —
   `returns`, физика/ledger — `inventory.return_*`; вьюха ledger не пишет
   (мок-тест §22.17).
2. `PartItem` `sold`/`installed` → `quarantine`/`available` и снова на складе;
   `StockLot.quantity` растёт (новый/оживлённый лот по §8); создаётся
   `RETURN_ITEM`/`RETURN_LOT` с `document=stock_return`; баланс пересобран.
3. Себестоимость заморожена из источника; `cost_total` на возврате; ретро-пересчёта
   нет.
4. Нельзя вернуть больше проданного/выданного; нельзя вернуть дважды; нельзя в
   неподходящую ячейку; проведённый возврат immutable.
5. Права: `MANAGE_RETURNS` (Админ/Руководитель/Кладовщик); Продавец/Мастер не
   проводит; себестоимость — под `can_view_purchase_cost`.
6. **Деньги не тронуты**: нет refund/кассы/чека/сторно; `Sale`/`RepairOrder`
   остаются `completed`, их финансовые итоги неизменны.
7. Границы: нет CRM/гарантии/аналитики/PDF; `StockBalance` не источник истины;
   `StockMovement` из вьюх не пишется.
8. Тесты зелёные (вкл. регресс продаж/ремонта); `ruff`/`djlint` чисты;
   `manage.py check` ок; `makemigrations --check` — миграции **только**
   `apps/returns` (+ seed) и **одна** `apps/inventory` (типы `RETURN_*`).

---

## 26. Файлы (создаются/изменяются)

**Изменяются — `apps/inventory`:**
- `models.py` — `MovementType += RETURN_ITEM, RETURN_LOT`.
- `migrations/0007_alter_stockmovement_movement_type.py`.
- `services.py` — `return_part_item`, `return_stock_lot_quantity`.

**Создаются — `apps/returns/`:**
- `__init__.py`, `apps.py`, `models.py` (`StockReturn`, `StockReturnLine`),
  `services.py`, `forms.py`, `views.py`, `urls.py`, `admin.py`.
- `migrations/__init__.py`, `migrations/0001_initial.py`,
  `migrations/0002_seed_return_sequence.py` (ключ `stock_return`, `RET-`).

**Изменяются — `apps/accounts`:**
- `roles.py` — `MANAGE_RETURNS` + привязка ролей (Админ/Руководитель/Кладовщик).
- `models.py` — `can_manage_returns`.
- `context_processors.py` — пункт «Возвраты».

**Изменяются — прочее:**
- `config/settings/base.py` — `LOCAL_APPS += "apps.returns"`.
- `config/urls.py` — `path("returns/", include("apps.returns.urls"))`.
- `templates/sales/sale_detail.html` — ссылка «Оформить возврат».
- `templates/repairs/repair_order_detail.html` — ссылка «Оформить возврат».

**Создаются — шаблоны `templates/returns/`:**
- `return_list.html`, `return_detail.html`, `return_form.html`.

**Тесты:** `tests/test_returns.py`.

**Без изменений:** `PartItem.Status` / `StockLot.Status` (целевые статусы уже есть);
финансовые итоги `Sale`/`RepairOrder`.

---

## 27. Что будет закоммичено

Два коммита (как в Слоях 5–17):
1. `План Слоя 18: возвраты на склад` — этот файл (push в `origin/main`).
2. `Слой 18: возвраты на склад` — реализация (после `pytest`, `ruff`, `djlint`,
   `makemigrations --check`, `manage.py check`), затем **push в `origin/main`**.

Останавливаемся перед **Слоем 19**.

---

## Границы Слоя 18 (чего НЕ делаем)

- Не реализуем денежный refund, кассу, чеки, гарантийный модуль, сторно продажи.
- Не меняем финансовые итоги `Sale` задним числом; не меняем статус `completed`
  у `Sale`/`RepairOrder`.
- Не делаем CRM, аналитику, PDF-документы.
- **Не пишем `StockMovement`/`StockBalance`/`PartItem.status`/`StockLot.quantity`
  напрямую из `apps/returns`** — только через `inventory.return_*`.
- Не превращаем `StockBalance` в источник истины.

---

## Решения (утверждены 2026-06-26)

Все рекомендации приняты заказчиком. Вопросы закрыты:

1. **Имя приложения:** `apps/returns` — ✅ принято (нейтральный модуль над
   sales+repairs+inventory; не смешивает складской возврат с денежным refund).
2. **Охват:** все 4 сценария (продажа/ремонт × экземпляр/лот) — ✅ принято
   (физика одна, отличается только источник).
3. **Тип движения:** `RETURN_ITEM`/`RETURN_LOT` (универсальный возврат) — ✅ принято;
   источник хранится в документе, движение отражает факт «деталь вернулась».
4. **Целевой статус экземпляра:** по умолчанию `quarantine`, `available` — только
   явным выбором; `PartItem.Status.RETURNED` **не используем** (вне
   `ITEM_PHYSICAL_STATUSES`, не восстановил бы остаток) — ✅ принято.
5. **Возврат количества в лот:** найти/оживить/создать в (`batch_line`,
   `to_location`); конфликт статуса → понятная ошибка, без смешивания; лот не
   дробим — ✅ принято (§8).
6. **Номер:** `RET-000001` (ASCII) — ✅ принято.
7. **Статусы возврата:** только `draft`/`completed`; отмена проведённого — будущий
   слой корректировок — ✅ принято.
8. **Право:** `MANAGE_RETURNS` для Админ/Руководитель/Кладовщик; Продавец/Мастер —
   только просмотр (без скрытой отмены продажи); Наблюдатель — просмотр;
   себестоимость под `can_view_purchase_cost` — ✅ принято.
9. **Деньги:** возврат только складской; итоги `Sale`/`RepairOrder` не трогаем,
   статус `completed` не меняем; refund/касса/чек/гарантия/сторно — будущие слои —
   ✅ принято. На карточках можно показать факт возврата, не меняя финансовую историю.
10. **Источник на строке:** `source_sale_line` XOR `source_repair_line` на каждой
    `StockReturnLine` + `source_type/source_id` на шапке — ✅ принято; нельзя вернуть
    больше, чем продано/выдано минус уже возвращено.
