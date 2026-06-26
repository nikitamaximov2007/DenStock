# План реализации — Слой 20. Инвентаризация и корректировки остатков

**Статус:** УТВЕРЖДЁН (2026-06-26) · все рекомендации приняты · реализация в границах §24.

---

## 1. Цель слоя

Сделать **документ инвентаризации**, который сверяет **фактическое наличие** с
системным и создаёт **контролируемые корректировки остатков** через
`adjust_in`/`adjust_out`. Это **акт сверки факта с системой**, а НЕ списание, не
возврат, не продажа и не ремонт.

- сверка: по каждому лоту видно **системное** количество (`expected`),
  **фактическое** (`counted`) и **расхождение** (`difference`);
- корректировка: при `counted ≠ expected` документ при проведении делает
  `ADJUST_IN`/`ADJUST_OUT`, чтобы `StockLot.quantity` стал равен `counted`;
- **узкая граница:** инвентаризация **количественных лотов** (StockLot) по ячейке,
  а не полная система аудита всего склада со сканером/фото/пересчётом каждого
  экземпляра. Поштучный `PartItem`-пересчёт в этот слой **не входит** (§13).

**Главная мысль (граница слоя):** Слой 20 — документ **сверки**. Он может создать
`ADJUST_IN`/`ADJUST_OUT` для лота, но **не заменяет** списания (Слой 19), возвраты
(Слой 18), продажи (Слой 16) и ремонт (Слой 17). Недостача/брак, найденные при
сверке, при необходимости оформляются своими документами; инвентаризация лишь
приводит **системное количество лота** к факту.

**Главный архитектурный контроль (как в Слоях 10/12/14–19):** физическая
корректировка и запись ledger идут **только через сервис `apps/inventory`**
(`adjust_stock_lot_quantity`). **Документ инвентаризации ведёт `apps/stocktaking`
сам, но `StockMovement`/`StockBalance`/`StockLot.quantity` напрямую НЕ пишет.**
Граница закрепляется тестом-моком (§24).

### Что уже есть (переиспользуем — Слой 20 в основном тонкий документ)

| Уже реализовано | Где | Роль в Слое 20 |
|---|---|---|
| **`adjust_stock_lot_quantity(lot, delta, *, by, comment)`** | `inventory/services.py:435` | готовый примитив корректировки: ADJUST_IN/OUT, `<0`→ошибка, **0→`depleted`**, обязательный комментарий |
| `MovementType.ADJUST_IN` / `ADJUST_OUT` | `inventory/models.py:141` | типы движения корректировки (новые **не добавляем**) |
| `StockMovement.document_type/document_id` + `_record_movement(...)` | `inventory` | связь движения с документом инвентаризации (нужна правка adjust, §15) |
| `_refresh_balance`, `recompute_balance_row`, `check_stock_balance`, `rebuild_stock_balance` | `inventory/services.py` | кэш пересобирается из первички; сверка кэша остаётся зелёной |
| Public `active_reserved_for_lot` | `apps/sales` (Слой 15) | запрет уводить лот ниже брони (§14) |
| `NumberSequence`, `money()`, `can_view_purchase_cost` | `inventory`/`procurement`/`accounts` | номер `IC-`, округление, скрытие себестоимости |
| Паттерн «документ = приложение + сервисы + UI» | sales/repairs/returns/writeoffs | архитектурный шаблон Слоя 20 |

### Что нового в Слое 20

- Новое приложение **`apps/stocktaking`**: модели `InventoryCountDocument`,
  `InventoryCountLine`, сервисы, вьюхи, шаблоны.
- **Маленькое расширение** `inventory.adjust_stock_lot_quantity`: опц. `document_id`
  (+ `document_type="inventory_count"`), чтобы корректировка ссылалась на документ.
  **Новых типов движения и миграций инвентаря НЕ требуется** (ADJUST_* уже есть).
- Возможность **`MANAGE_STOCKTAKING`**, экраны инвентаризации, действие
  «Инвентаризировать» из карточки лота.

**Чего слой НЕ делает:** продажа, ремонт, возврат, списание, оплата, касса, чеки,
refund, гарантия, аналитика, бухгалтерия, PDF, **сканерная инвентаризация**,
**поштучный `PartItem`-пересчёт**, создание новых `PartType`/`BatchLine` «из
воздуха», полный авто-пересчёт всего склuda; не превращает `StockBalance` в источник
истины; не пишет `StockMovement` из вьюх.

---

## 2. Где размещаем домен — **рекомендация: отдельное `apps/stocktaking`**

| Вариант | За | Против | Вывод |
|---|---|---|---|
| **`apps/stocktaking` (рекомендация)** | инвентаризация — самостоятельный документ сверки; повторяет паттерн sales/repairs/returns/writeoffs; ацикличные зависимости | новое приложение | **выбираем** |
| `apps/inventory` (внутри) | примитив корректировки рядом | документ сверки (scope/статусы/строки/expected-counted) — не дело ledger-слоя; раздуло бы inventory; смешало бы «движок остатка» и «бизнес-документ» | только **расширение adjust** (§15) |
| `apps/audits` | обобщённо | «аудит» шире инвентаризации; преждевременное обобщение | нет |

**Обоснование.** Инвентаризацию важно отделить от обычных складских операций:
это **отдельный документ сверки** со своим статусом, ячейкой-областью и строками
expected/counted. Отдельное `apps/stocktaking` даёт документ, а физику отдаёт
готовому `inventory.adjust_stock_lot_quantity`. Зависимости: `stocktaking →
inventory` (adjust + StockLot) и `stocktaking → sales` (только public-проверка
брони, §14) — **ацикличны**.

---

## 3. Сущности и scope

Две модели в `apps/stocktaking/models.py`.

### 3.1 `InventoryCountDocument` (шапка)

**Scope — targeted по ячейке (рекомендация).** `scope_location` (FK
`StorageLocation`, nullable) — ячейка под инвентаризацию; строки добавляются по
лотам этой ячейки. **Полный авто-пересчёт всего склада/категории — будущее**
(он порождает сотни строк и сложный UI; в этот слой не входит). Без scope_location
допускаются ручные точечные строки (любой физический лот), но рекомендуемый поток —
«инвентаризация ячейки X».

### 3.2 `InventoryCountLine` (строка)

**Только `StockLot`** (количественная инвентаризация). Поштучный `PartItem` в этот
слой не входит (§13). Хранит снимок системного количества, введённый факт и (после
проведения) ссылку на созданное движение корректировки.

---

## 4. Поля `InventoryCountDocument`

| Поле | Тип | Назначение |
|---|---|---|
| `number` | `CharField` unique, editable=False (`IC-000001`, ASCII) | номер документа |
| `status` | `CharField(choices=Status)` | `draft`/`completed`/`canceled` (§5) |
| `scope_location` | FK `warehouse.StorageLocation`, `PROTECT`, null/blank | ячейка-область сверки (подсказка строк) |
| `comment` | `CharField` blank | примечание |
| `created_by` | FK user, `SET_NULL` | кто создал |
| `created_at` / `updated_at` | auto | аудит |
| `completed_at` | `DateTimeField` null/blank | момент проведения |
| `canceled_at` | `DateTimeField` null/blank | момент отмены (черновика) |

Номер `IC-000001` — ASCII (Inventory Count), коротко и однозначно, единообразно с
`S-`/`R-`/`RET-`/`WO-`. Отдельный ключ `NumberSequence "inventory_count"` (seed-
миграция, prefix `IC-`).

---

## 5. Поля `InventoryCountLine` и статусы документа

### 5.1 `InventoryCountLine`

| Поле | Тип | Назначение |
|---|---|---|
| `count_document` | FK `InventoryCountDocument`, `CASCADE`, related_name="lines" | шапка |
| `stock_lot` | FK `inventory.StockLot`, `PROTECT`, related_name="count_lines" | инвентаризируемый лот |
| `part_type` / `batch_line` / `location` | FK `PROTECT` | денормализация (деталь/строка партии/ячейка) |
| `expected_quantity` | `Decimal(12,3)` | **снимок** системного кол-ва на момент добавления (информативно) |
| `counted_quantity` | `Decimal(12,3)` null/blank | фактическое кол-во (вводится при сверке) |
| `unit_cost_rub` | `Decimal(12,2)` editable=False | landed cost лота (для оценки расхождения в ₽) |
| `adjustment` | FK `inventory.StockMovement`, `PROTECT`, null/blank, related_name="+" | созданное движение (при `counted ≠ live`) |
| `created_at` | auto | аудит |

**Ограничения БД:** `UniqueConstraint(count_document, stock_lot)` (один лот — одна
строка в документе); `CheckConstraint counted_quantity >= 0` (когда задано — через
`Q(counted_quantity__isnull=True) | Q(counted_quantity__gte=0)`).

**`difference` (property):** `counted_quantity − expected_quantity` (None, если не
сосчитано); знак показывает недостачу (−) / излишек (+). Это **дисплейная** величина;
фактическая дельта корректировки считается от **живого** `lot.quantity` (§7/§9).

### 5.2 Статусы документа — минимальный набор

```python
class Status(models.TextChoices):
    DRAFT     = "draft",     "Черновик"    # вводим/правим counted; склад НЕ трогаем
    COMPLETED = "completed", "Проведён"     # расхождения применены (ADJUST_*)
    CANCELED  = "canceled",  "Отменён"      # черновик отменён (склад не трогали)
```

**Рекомендация:** `draft → completed`; `draft → canceled` (отмена **черновика**).
Отдельный статус `counting` **не вводим** — фаза ввода `counted` укладывается в
`draft` (строки добавляются и правятся, пока документ — черновик); это упрощает слой
без потери смысла. Отмена **проведённой** инвентаризации (откат корректировок) — это
новый встречный документ инвентаризации, а не правка; проведённый документ
**immutable** (зеркально Слоям 16–19).

---

## 6. Что инвентаризируем

- **`StockLot` по количеству**, в рамках ячейки (`scope_location`) или точечно.
- Пользователь добавляет строки **существующих** лотов и вводит `counted_quantity`.
- **Без автогенерации всего склада** и **без поштучного `PartItem`** (§13).
- Лоты под инвентаризацию — только в **физическом** статусе
  (`available`/`quarantine`/`receiving`); `depleted`/`written_off` не считаем
  (оживление нулевого лота = «сток из воздуха», вне слоя — §11).

---

## 7. Как считать системное количество (source of truth — первичка)

- **Системное количество строки = `StockLot.quantity` первичного объекта.**
- `expected_quantity` строки — это **снимок** `lot.quantity` на момент добавления
  (для отображения и расчёта `difference`).
- **На проведении источник истины — живой `lot.quantity`** (перечитанный под
  `select_for_update`): дельта корректировки = `counted − live_lot.quantity`. Так
  если между сверкой и проведением лот изменился (продажа/выдача), документ всё
  равно приведёт количество к факту корректно (см. §9, TOCTOU).
- `StockBalance` можно использовать как **read-оптимизированную подсказку** в UI, но
  **в расчёте расхождения он не участвует** и источником истины не является.

---

## 8. Как фиксируем факт и расхождение

**Только `StockLot`** (рекомендация §13). Для строки:

| Величина | Откуда |
|---|---|
| `expected_quantity` | снимок `lot.quantity` при добавлении строки |
| `counted_quantity` | вводит кладовщик при сверке (≥ 0) |
| `difference` (display) | `counted − expected` |
| дельта корректировки (apply) | `counted − live_lot.quantity` (на проведении) |

**Обоснование модели строк.** Одна строка = один лот; `expected` фиксируем при
добавлении (чтобы видеть, «что показывала система, когда начали считать»), а
применяем разницу от живого количества — это безопасно при гонке и даёт ровно
«привести систему к факту». `PartItem` поштучно не моделируем — это держит слой
узким (одна строка-тип, одна корректировка-тип).

---

## 9. Как создаются корректировки (только StockLot)

При проведении по каждой строке (под блокировкой лота):

1. `delta = counted_quantity − lot.quantity` (живой, перечитан `select_for_update`).
2. **`delta == 0`** → движение **не создаётся** (факт совпал с системой).
3. **`delta > 0`** (излишек) → `inventory.adjust_stock_lot_quantity(lot, +delta, …)`
   → `ADJUST_IN`; количество растёт **по существующему `landed_unit_cost_rub` лота**.
4. **`delta < 0`** (недостача) → `adjust_stock_lot_quantity(lot, −|delta|, …)` →
   `ADJUST_OUT`; при нуле статус → **`depleted`** (см. §12).

**`PartItem` missing/extra** в этот слой не входит: подтверждённую недостачу
экземпляра оформляют **списанием (Слой 19)**, излишек/возврат экземпляра — приёмкой/
возвратом. Отдельный `inventory_loss`/`inventory_missing` тип движения **не вводим**.

**TOCTOU-защита.** `complete_inventory_count` сам перечитывает лот
`select_for_update`, считает `delta` от заблокированного количества и вызывает
`adjust_*` в той же транзакции (повторный `select_for_update` на той же строке
безопасен) — результат гарантированно равен `counted`.

---

## 10. `StockMovement`

| Поле движения | Значение |
|---|---|
| `movement_type` | **`ADJUST_IN`** (delta>0) / **`ADJUST_OUT`** (delta<0) |
| `from_location` / `to_location` | OUT: `lot.location`→`null`; IN: `null`→`lot.location` |
| `quantity` | `abs(delta)` |
| `unit_cost_rub` | `lot.landed_unit_cost_rub` (существующий landed cost лота) |
| `total_cost_rub` | `unit_cost × abs(delta)` (в `StockMovement.save()`) |
| `document_type` | **`"inventory_count"`** |
| `document_id` | `InventoryCountDocument.id` |

**Себестоимость `ADJUST_IN` — из самого лота.** Так как корректируем **существующий**
лот (§11), при увеличении количество добавляется по **его собственному**
`landed_unit_cost_rub` — никакого «нового landed из воздуха» не нужно. `ADJUST_OUT`
аналогично берёт landed лота. Это снимает вопрос §10 о цене прихода: цена прихода =
цена этого лота.

**Используем существующие `ADJUST_IN`/`ADJUST_OUT`** — новых типов и миграции
инвентаризации не требуется. Нужна лишь передача `document_type/document_id` в
движение (§15).

---

## 11. Ограничение: только существующие лоты (без «стока из воздуха»)

- Корректировки делаем **только по уже существующим `StockLot`**:
  - факт меньше → уменьшаем существующий лот (`ADJUST_OUT`);
  - факт больше → увеличиваем существующий лот по его же `batch_line`/
    `landed_unit_cost_rub` (`ADJUST_IN`).
- **Не создаём** новые `PartType`/`BatchLine`/`StockLot` во время инвентаризации —
  это безопаснее и не плодит «неизвестные детали из воздуха». Найденная неизвестная
  деталь оформляется обычной приёмкой (Слои 8–10), а не инвентаризацией.

---

## 12. Влияние на `StockLot`

- `counted < expected` → `quantity` уменьшается; `counted > expected` → растёт.
- **При `quantity == 0` статус → `depleted`** (поведение `adjust_stock_lot_quantity`
  уже такое).
- **НЕ `written_off`** — это не списание по причине (Слой 19), а корректировка сверки;
  отдельный `inventory_zero` статус не вводим (лишняя сущность). `depleted`
  семантически подходит: «лота физически нет», без причины потери.
- Частичная корректировка статус не меняет (лот остаётся `available`/`quarantine`).

---

## 13. Влияние на `PartItem` — **в Слой 20 не входит (рекомендация)**

- Поштучную инвентаризацию `PartItem` **не делаем**: ни физической корректировки, ни
  discrepancy-строк. Причина — узость слоя и нежелание плодить спорные статусы
  (`inventory_missing`/`inventory_extra`) и логику «экземпляр из воздуха».
- Найденное **отсутствие** экземпляра оформляется **списанием (Слой 19)** —
  осознанным документом с причиной (`lost`); **излишек** экземпляра — обычной
  приёмкой/возвратом. Эти потоки уже есть.
- Так Слой 20 остаётся узким: **инвентаризация количественных лотов**. Поштучный
  пересчёт со сканером — отдельный будущий слой.

---

## 14. Резервы

- **Нельзя корректировкой увести `lot.quantity` ниже активной брони:** на проведении
  для строки с `counted < reserved` (где `reserved = active_reserved_for_lot(lot)`)
  — **ошибка** `StocktakingError`: «факт меньше зарезервированного — сначала решите
  бронь вручную».
- Инвентаризация **не отменяет** резервы автоматически.
- Проверка — через public `apps.sales.active_reserved_for_lot` (как в Слоях 17–19);
  зависимость `stocktaking → sales` ациклична.

> Примитив `adjust_stock_lot_quantity` гарантирует только `quantity ≥ 0`; «не ниже
> брони» — забота `apps/stocktaking` (слой-вызыватель), как и в продаже/ремонте/
> списании.

---

## 15. Сервис `apps/inventory` — маленькое расширение

`adjust_stock_lot_quantity` **уже существует** и делает ровно нужное (ADJUST_*,
`<0`→ошибка, `0`→`depleted`, обязательный комментарий, `_refresh_balance`).
Две правки — **проброс ссылки на документ** и **возврат движения**:

```python
# было:  def adjust_stock_lot_quantity(lot, delta, *, by=None, comment="") -> StockLot:
#            ... _record_movement(...); return lot
# станет: def adjust_stock_lot_quantity(lot, delta, *, by=None, comment="",
#                                       document_type=None, document_id=None) -> StockMovement:
#            mv = _record_movement(..., document_type=document_type, document_id=document_id)
#            ... return mv
```

- Опц. `document_type`/`document_id` с дефолтами `None` → **обратная совместимость**
  по поведению (текущие вызовы/ручные корректировки пишут движение как раньше).
- **Возврат меняется `StockLot` → `StockMovement`** — это безопасно: **ни один из
  двух существующих вызывающих не использует возвращаемое значение**
  (`inventory/views.py:546` игнорирует результат; `test_stock_movements.py` после
  вызова делает `lot.refresh_from_db()`). `_record_movement` уже возвращает созданное
  движение, так что правка минимальна.
- `apps/stocktaking` вызывает с `document_type="inventory_count"`, `document_id=doc.pk`
  и пишет результат в `InventoryCountLine.adjustment`.
- **Новых функций/типов движения/миграций инвентаря не требуется.**

---

## 16. Сервисы `apps/stocktaking/services.py`

```python
class StocktakingError(Exception): ...

create_inventory_count(*, scope_location=None, comment="", by) -> InventoryCountDocument
add_stock_lot_count_line(doc, lot, *, by) -> InventoryCountLine   # снимок expected, заморозка cost
update_counted_quantity(line, counted, *, by) -> InventoryCountLine  # draft, counted >= 0
remove_count_line(line, *, by)
complete_inventory_count(doc, *, by) -> InventoryCountDocument    # применяет расхождения
cancel_inventory_count(doc, *, by)                               # draft → canceled
count_discrepancy_summary(doc) -> dict                           # для UI (итоги/₽), если нужно
```

**`complete_inventory_count` (оркестрация, `@transaction.atomic`):**
1. lock документ; должен быть `draft`; строк ≥ 1 (иначе `StocktakingError`).
2. каждая строка **должна** иметь `counted_quantity` (иначе ошибка «не все строки
   сосчитаны»).
3. по каждой строке: lock `lot` (`select_for_update`); `delta = counted − lot.quantity`;
   - `delta == 0` → пропуск (движение не создаём);
   - `delta < 0` и `counted < active_reserved_for_lot(lot)` → `StocktakingError`;
   - иначе `mv = adjust_stock_lot_quantity(lot, delta, by=by,
     comment=f"Инвентаризация {doc.number}", document_type="inventory_count",
     document_id=doc.pk)`; `line.adjustment = mv` (adjust возвращает движение).
4. `status=completed`, `completed_at=now`.

> Привязка движения к строке (утверждено): `adjust_stock_lot_quantity` возвращает
> созданное `StockMovement`; `complete` пишет его в `line.adjustment`.

- Все мутации — **только через `inventory.adjust_stock_lot_quantity`**; вьюхи ledger
  не пишут (тест §24).

---

## 17. Права

Новая возможность **`MANAGE_STOCKTAKING`** (`roles.py`):

| Роль | Создавать/проводить | Видеть | Себестоимость |
|---|---|---|---|
| Администратор | ✅ | ✅ | ✅ |
| Руководитель | ✅ | ✅ | ✅ |
| **Кладовщик** | ✅ | ✅ | нет |
| Продавец/Мастер | ❌ | ✅ | нет |
| Наблюдатель | ❌ | ✅ | по `can_view_purchase_cost` |

**Обоснование.** Инвентаризация — складское действие (физический пересчёт ячеек),
поэтому право даём Админу/Руководителю/**Кладовщику**. Продавец/Мастер —
только просмотр; Наблюдатель — просмотр. Себестоимость расхождений — под
`can_view_purchase_cost`.

- `roles.py`: `MANAGE_STOCKTAKING` для `ADMIN`/`MANAGER`/`STOREKEEPER`.
- `accounts/models.py`: `can_manage_stocktaking`. Без миграции (возможности — код).
- Просмотр — `login_required`; мутации — под `manage_stocktaking`.

---

## 18. UI (`apps/stocktaking`, шаблоны `templates/stocktaking/`)

| Экран | URL (`name`) | Право |
|---|---|---|
| Список инвентаризаций | `/stocktaking/` (`inventory_count_list`) | просмотр — вошедшие |
| Карточка документа | `…/<pk>/` (`inventory_count_detail`) | просмотр — вошедшие |
| Создать | `…/new/` (`inventory_count_create`) | `manage_stocktaking` |
| Добавить лот | POST `…/<pk>/add-lot/` | `manage_stocktaking` |
| Ввести факт по строке | POST `…/lines/<pk>/count/` | `manage_stocktaking` |
| Снять строку | POST `…/lines/<pk>/remove/` | `manage_stocktaking` |
| Провести | POST `…/<pk>/complete/` | `manage_stocktaking` |
| Отменить (черновик) | POST `…/<pk>/cancel/` | `manage_stocktaking` |

- Создание: выбор `scope_location` (ячейка) и комментария.
- Карточка: строки лотов с `expected` / поле ввода `counted` / `difference` (цветом
  недостача/излишек/ок); **себестоимость расхождения и итоги — только
  `can_view_purchase_cost`**; кнопка «Добавить лот» (лоты ячейки в физ. статусе).
- Проведённый документ — без кнопок правки (immutable).
- No-JS: server-rendered формы; ввод `counted` — отдельная мини-форма на строку.

---

## 19. Scanner — **не трогаем**

- `StockLot` не имеет штрихкода; сканерный поток для лотов нерелевантен.
- Поштучного `PartItem`-пересчёта в слое нет (§13), поэтому сканер не нужен.
- **Сканерную инвентаризацию оставляем будущему слою** — в Слое 20 обычный UI.

---

## 20. Интеграция

- Ссылка **«Инвентаризировать»** из карточки `lot_detail` для ролей с
  `manage_stocktaking` (создаёт/открывает документ по ячейке лота).
- После проведения `/search/` показывает **обновлённый остаток** (кэш пересобран
  сервисом).
- **`check_stock_balance` остаётся зелёным** после проведения (корректировки идут
  через первичку + `_refresh_balance`).

---

## 21. Инварианты (и кто гарантирует)

| Инвариант | Гарант |
|---|---|
| Нельзя провести пустую инвентаризацию | `complete`: строк ≥ 1 |
| Все строки должны быть сосчитаны | `complete`: `counted_quantity` не null |
| Нельзя провести документ дважды | `complete` требует `draft` |
| Проведённый документ **immutable** | сервисы: мутации только при `draft` |
| `counted_quantity` не отрицательно | сервис + `CheckConstraint` |
| Нельзя увести `StockLot` ниже 0 | `adjust_stock_lot_quantity`: `new_qty ≥ 0` |
| Нельзя увести ниже брони | `stocktaking`: `counted ≥ active_reserved_for_lot` |
| `counted == live` → движения нет | `complete`: `delta == 0` → пропуск |
| Корректировка создаёт `ADJUST_IN`/`ADJUST_OUT` | `inventory.adjust_stock_lot_quantity` |
| Корректировка меняет physical `quantity`; баланс пересобран | `adjust_*` + `_refresh_balance` |
| Инвентаризация **не** создаёт `Sale`/`RepairOrder`/`StockReturn`/`WriteOffDocument` | границы |
| Инвентаризация **не** создаёт оплату/чек/refund | границы |

---

## 22. Транзакции и блокировки

- `complete_inventory_count` — целиком в `transaction.atomic`.
- `select_for_update` на `InventoryCountDocument` и на каждом `StockLot` (дельта
  считается от заблокированного количества — TOCTOU §9); `adjust_*` повторно блокирует
  ту же строку в той же транзакции (безопасно).
- **Защита от двойного проведения:** `complete` требует `draft`.
- **Защита от ухода в минус / ниже брони** — §14/§21 под блокировкой.
- Конкурентный Postgres-тест — будущий слой (тестовый стек SQLite).

---

## 23. Management-команды

- **Не требуются.** Сверка кэша — существующий `check_stock_balance`; пересборка —
  `rebuild_stock_balance`. Корректировки уже отражены в первичке (`StockLot.quantity`)
  и движениях.
- Отдельный `check_inventory_counts` **не нужен**: документ — не источник остатка, а
  журнал сверки; целостность остатка проверяет `check_stock_balance`.

---

## 24. Тесты (`tests/test_stocktaking.py`)

1. Можно создать черновик инвентаризации (`draft`, номер `IC-`).
2. Нельзя провести пустой документ (`StocktakingError`).
3. Можно добавить строку лота (снимок `expected`, заморозка `unit_cost`).
4. `counted == expected` → движения **не создаются**, `quantity` без изменений.
5. `counted < expected` → `ADJUST_OUT`; `StockLot.quantity` уменьшается до `counted`.
6. `counted > expected` → `ADJUST_IN`; `StockLot.quantity` растёт до `counted`.
7. Движение имеет `document_type="inventory_count"`, `document_id=doc.id`,
   корректные `from/to_location`.
8. При `counted == 0` лот → статус **`depleted`**.
9. Нельзя `counted_quantity < 0`.
10. Нельзя свести лот ниже брони (`counted < active_reserved`).
11. Нельзя провести документ с несосчитанной строкой.
12. Нельзя провести дважды (`completed` immutable; `remove`/`cancel`/`count` тоже).
13. **Архитектурный мок:** при проведении вьюха вызывает сервис и сама
    `StockMovement`/`StockBalance` не пишет (`patch` сервиса → ledger неизменен).
14. Hidden/query-параметры перепроверяются сервером (подмена документа/строки/лота/
    counted → ошибка/404, без эффекта).
15. Пользователь с `manage_stocktaking` (Кладовщик) может провести.
16. Пользователь без права (Продавец/Мастер) — **403** (но видит список).
17. Себестоимость скрыта без `can_view_purchase_cost`; видна Руководителю.
18. Инвентаризация **не** создаёт `Sale`/`RepairOrder`/`StockReturn`/`WriteOffDocument`.
19. **`check_stock_balance()` пуст (зелёный) после проведения** (кэш = первичка).
20. Регресс: существующие тесты складского ядра/adjust остаются зелёными после
    расширения `adjust_stock_lot_quantity` (`document_*`).

---

## 25. Ручная проверка

1. Кладовщиком → карточка лота → «Инвентаризировать» (или `/stocktaking/` → создать,
   выбрать ячейку).
2. Добавить лоты ячейки; ввести `counted`: равный системе (нет движения), меньше
   (недостача), больше (излишек) → провести.
3. В «Движениях» — `ADJUST_OUT`/`ADJUST_IN` с `document=inventory_count`; `/search/`
   показывает обновлённый остаток.
4. Свести лот в 0 → лот `depleted`.
5. Попробовать `counted` ниже брони → отказ (решите бронь).
6. Себестоимость расхождений: кладовщику не видна; админу — видна.
7. Продавцом/Мастером → проведение недоступно (403), список виден.
8. `python manage.py check_stock_balance` → расхождений нет.

---

## 26. Критерии готовности

1. Корректировка идёт **только через сервис**: документ — `stocktaking`, физика/
   ledger — `inventory.adjust_stock_lot_quantity`; вьюха ledger не пишет (мок §24.13).
2. `counted` приводит `StockLot.quantity` к факту: `>` → `ADJUST_IN`, `<` →
   `ADJUST_OUT`, `==` → без движения; `0` → `depleted`; `document=inventory_count`.
3. Нельзя ниже 0 и ниже брони; нельзя пустую/несосчитанную/повторную; проведённый
   immutable.
4. Только существующие лоты; новые `PartType`/`BatchLine`/`StockLot` не создаются;
   поштучный `PartItem` не трогаем.
5. Права: `MANAGE_STOCKTAKING` (Админ/Руководитель/Кладовщик); Продавец/Мастер не
   проводит; себестоимость — под `can_view_purchase_cost`.
6. Границы: нет продажи/ремонта/возврата/списания/оплаты/аналитики/PDF/сканера;
   `StockBalance` не источник истины; `StockMovement` из вьюх не пишется.
7. `check_stock_balance` зелёный после проведения.
8. Тесты зелёные (вкл. регресс ядра/adjust); `ruff`/`djlint` чисты;
   `manage.py check` ок; `makemigrations --check` — миграции **только**
   `apps/stocktaking` (+ seed). **Миграции инвентаря не требуется** (ADJUST_* уже есть).

---

## 27. Файлы (создаются/изменяются)

**Изменяется — `apps/inventory`:**
- `services.py` — `adjust_stock_lot_quantity` получает опц. `document_type`/
  `document_id` (проброс в `_record_movement`). **Без миграции.**

**Создаются — `apps/stocktaking/`:**
- `__init__.py`, `apps.py`, `models.py` (`InventoryCountDocument`,
  `InventoryCountLine`), `services.py`, `forms.py`, `views.py`, `urls.py`, `admin.py`.
- `migrations/__init__.py`, `migrations/0001_initial.py`,
  `migrations/0002_seed_inventory_count_sequence.py` (ключ `inventory_count`, `IC-`).

**Изменяются — `apps/accounts`:**
- `roles.py` — `MANAGE_STOCKTAKING` + привязка (Админ/Руководитель/Кладовщик).
- `models.py` — `can_manage_stocktaking`.
- `context_processors.py` — пункт «Инвентаризация».

**Изменяются — прочее:**
- `config/settings/base.py` — `LOCAL_APPS += "apps.stocktaking"`.
- `config/urls.py` — `path("stocktaking/", include("apps.stocktaking.urls"))`.
- `templates/inventory/lot_detail.html` (или эквивалент) — ссылка «Инвентаризировать».

**Создаются — шаблоны `templates/stocktaking/`:**
- `inventory_count_list.html`, `inventory_count_detail.html`,
  `inventory_count_form.html`.

**Тесты:** `tests/test_stocktaking.py`.

**Без изменений:** `MovementType.ADJUST_IN/ADJUST_OUT` (уже есть); `StockLot.Status`.

---

## 28. Что будет закоммичено

Два коммита (как в Слоях 5–19):
1. `План Слоя 20: инвентаризация и корректировки` — этот файл (push в `origin/main`).
2. `Слой 20: инвентаризация и корректировки` — реализация (после `pytest`, `ruff`,
   `djlint`, `makemigrations --check`, `manage.py check`), затем **push в `origin/main`**.

Останавливаемся перед **Слоем 21**.

---

## Границы Слоя 20 (чего НЕ делаем)

- Не реализуем продажу, ремонт, возврат, списание, оплату, кассу, чеки, refund,
  гарантию, аналитику, бухгалтерию, PDF, **сканерную инвентаризацию**.
- **Не пишем `StockMovement`/`StockBalance`/`StockLot.quantity` напрямую из
  `apps/stocktaking`** — только через `inventory.adjust_stock_lot_quantity`.
- **Не создаём** новые `PartType`/`BatchLine`/`StockLot` из инвентаризации.
- **Не инвентаризируем `PartItem` поштучно** (недостача → списание Слоя 19).
- Не превращаем `StockBalance` в источник истины.

---

## Решения (утверждены 2026-06-26)

Все рекомендации приняты заказчиком. Вопросы закрыты:

1. **Имя приложения:** `apps/stocktaking` — ✅ принято (отдельный документ сверки,
   не продажа/ремонт/возврат/списание и не ручная правка склада).
2. **Охват:** только количественный `StockLot`; `PartItem` поштучно **не** входит
   (недостача экземпляра → списание Слоя 19; лишний экземпляр — будущее) — ✅ принято.
3. **Scope:** targeted/manual по ячейке (`scope_location`) + строки существующих
   лотов; полный авто-пересчёт склада — будущее — ✅ принято.
4. **Корректировки:** только существующие лоты; без новых `PartType`/`BatchLine`/
   партий/`StockLot`; `ADJUST_IN` по `landed_unit_cost_rub` самого лота — ✅ принято.
5. **Статус лота при нуле:** `depleted` (не `written_off`, не новый `inventory_zero`)
   — ✅ принято.
6. **Статусы документа:** `draft`/`completed` + отмена черновика (`canceled`); без
   `counting` — ✅ принято.
7. **Сервис inventory:** расширить `adjust_stock_lot_quantity` опц. `document_type`/
   `document_id` обратносовместимо **и вернуть созданное `StockMovement`**; без новых
   типов движения — ✅ принято.
8. **Право:** `MANAGE_STOCKTAKING` для Админ/Руководитель/Кладовщик; Продавец/Мастер —
   только просмотр; Наблюдатель — просмотр; себестоимость под `can_view_purchase_cost`
   — ✅ принято.
9. **Резервы:** запрет свести лот ниже `active_reserved_qty`, без авто-отмены резерва
   — ✅ принято.
10. **Номер:** `IC-000001` (ASCII) — ✅ принято.
11. **Привязка движения к строке:** `adjust_stock_lot_quantity` возвращает созданное
    `StockMovement`, `InventoryCountLine.adjustment` хранит ссылку — ✅ принято.
