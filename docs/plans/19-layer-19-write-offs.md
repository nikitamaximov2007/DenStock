# План реализации — Слой 19. Списания (документированное складское выбытие)

**Статус:** УТВЕРЖДЁН (2026-06-26) · все рекомендации приняты · реализация в границах §19.

---

## 1. Цель слоя

Сделать **контролируемое списание детали со склада** по причинам: **брак,
потеря, повреждение, утилизация, неликвид, служебное списание**. Списание:

- **уменьшает физический остаток** (`PartItem → written_off` / `StockLot.quantity↓`);
- создаёт **`StockMovement` списания** (`WRITE_OFF_ITEM`/`WRITE_OFF_LOT`);
- **фиксирует себестоимость** выбывшего (потери по себестоимости, заморожено);
- **фиксирует причину** (по какой беде ушла деталь).

**Главная мысль (граница слоя):** Слой 19 — это **документированное складское
списание**, а НЕ продажа, ремонт, возврат, **инвентаризация** или финансовый
документ оплаты. Любая потеря/брак/утилизация идёт **документом с причиной**, а не
ручной правкой `StockLot.quantity`/`PartItem.status`.

**Главный архитектурный контроль (как в Слоях 10/12/14/15/16/17/18):** физическое
выбытие и запись ledger идут **только через сервисы `apps/inventory`** (новые
`write_off_part_item` / `write_off_stock_lot_quantity`). **Документ списания ведёт
`apps/writeoffs` сам, но `StockMovement`/`StockBalance`/`PartItem.status`/
`StockLot.quantity` напрямую НЕ трогает.** Граница закрепляется тестом-моком (§23).

### Что уже есть (переиспользуем)

| Уже реализовано | Где | Роль в Слое 19 |
|---|---|---|
| `PartItem.Status.WRITTEN_OFF` / `StockLot.Status.WRITTEN_OFF` | `inventory/models.py` | целевой статус списанного (модели **не меняем**) |
| Приватные `_consume_part_item` / `_consume_stock_lot` | `inventory/services.py` (Слой 16/17) | общая механика расхода — параметризуем (§14) |
| `StockMovement.document_type/document_id` + `_record_movement(...)` | `inventory` | связь движения с `WriteOffDocument` |
| `ITEM/LOT_PHYSICAL_STATUSES` (written_off/depleted — вне их) | `inventory/services.py` | списание корректно убирает из physical/available |
| Public `is_part_item_reserved` / `active_reserved_for_lot` | `apps/sales` (Слой 15/17) | запрет списания зарезервированного (§12) |
| `NumberSequence`, `money()`, `can_view_purchase_cost` | `inventory`/`procurement`/`accounts` | номер `WO-`, округление, скрытие себестоимости |
| Паттерн «документ = приложение + сервисы + UI» (repairs) | `apps/repairs` (Слой 17) | архитектурный шаблон Слоя 19 |

### Что нового в Слое 19

- Новое приложение **`apps/writeoffs`**: модели `WriteOffDocument`, `WriteOffLine`,
  сервисы, вьюхи, шаблоны.
- Новые типы движения **`WRITE_OFF_ITEM` / `WRITE_OFF_LOT`** (`inventory.MovementType`)
  → **миграция инвентаря** `0008` (`AlterField` choices).
- Сервисы `write_off_part_item` / `write_off_stock_lot_quantity` в `apps/inventory`
  (физика + ledger, источник-агностичны) — через **параметризованные** `_consume_*`.
- Возможность **`MANAGE_WRITE_OFFS`**, экраны списаний, действие «Списать» в `/search/`.

**Чего слой НЕ делает:** продажа, ремонт, возврат, оплата, касса, чеки, refund,
гарантия, **инвентаризация**, полноценная аналитика потерь, бухгалтерские проводки,
PDF, CRM; не превращает `StockBalance` в источник истины; не пишет `StockMovement`
из вьюх; не использует `adjust_out` как пользовательское списание (§21).

---

## 2. Где размещаем домен — **рекомендация: отдельное `apps/writeoffs`**

| Вариант | За | Против | Вывод |
|---|---|---|---|
| **`apps/writeoffs` (рекомендация)** | списание — самостоятельный документ с причиной/статусом/строками; повторяет паттерн sales/repairs/returns; ацикличные зависимости | новое приложение | **выбираем** |
| `apps/inventory` (модели документа внутри) | физика рядом | документ списания (причина/статус/строки) — не дело ledger-слоя; раздуло бы inventory; смешало бы «движок остатка» и «бизнес-документ» | только **физические сервисы** (§14) |
| ручная правка `StockLot`/`PartItem` | «быстро» | **именно этого избегаем** — потеря без документа/причины/аудита | нет |

**Обоснование.** Главная цель — чтобы списание было **документом**, а не ручной
правкой остатков. Отдельное `apps/writeoffs` даёт документ (причина, статус, строки,
себестоимость потерь), а физику отдаёт источник-агностичным сервисам inventory.
Зависимости: `writeoffs → inventory` (FK + сервисы) и `writeoffs → sales` (только
public-проверки резерва, §12) — **ацикличны** (inventory/sales про writeoffs не знают).

---

## 3. Сущности

Две модели в `apps/writeoffs/models.py`. **Без бухгалтерии и финансовых проводок.**

### 3.1 `WriteOffDocument` (шапка списания)

Причина — **на документе** (одно списание = одна причина): `reason` ∈ перечисления
§6. Один документ может списывать несколько экземпляров/лотов по этой причине.

### 3.2 `WriteOffLine` (строка списания)

XOR `part_item`/`stock_lot` (как `SaleLine`/`RepairIssueLine`). Замораживает
себестоимость. **Без цены/прибыли** — это потеря, а не продажа.

---

## 4. Поля `WriteOffDocument`

| Поле | Тип | Назначение |
|---|---|---|
| `number` | `CharField` unique, editable=False (`WO-000001`, ASCII) | номер документа |
| `status` | `CharField(choices=Status)` | `draft`/`completed`/`canceled` (§5) |
| `reason` | `CharField(choices=Reason)` | причина списания (§6), обязательна |
| `comment` | `CharField` blank | примечание (детали потери) |
| `cost_total` | `Decimal(14,2)` default 0 | сумма себестоимости списанного (заморожена) |
| `created_by` | FK user, `SET_NULL` | кто создал |
| `created_at` / `updated_at` | auto | аудит |
| `completed_at` | `DateTimeField` null/blank | момент проведения |
| `canceled_at` | `DateTimeField` null/blank | момент отмены (черновика) |

Номер `WO-000001` — ASCII (Write-Off), коротко и однозначно, единообразно с
`S-`/`R-`/`RET-` (удобно для ссылок/поиска/логов). Отдельный ключ
`NumberSequence "write_off"` (seed-миграция, prefix `WO-`).

---

## 5. Поля `WriteOffLine` и статусы документа

### 5.1 `WriteOffLine`

| Поле | Тип | Назначение |
|---|---|---|
| `write_off` | FK `WriteOffDocument`, `CASCADE`, related_name="lines" | шапка |
| `part_type` | FK `catalog.PartType`, `PROTECT` | денормализация |
| `part_item` | FK `inventory.PartItem`, `PROTECT`, null | поштучно (XOR) |
| `stock_lot` | FK `inventory.StockLot`, `PROTECT`, null | количественно (XOR) |
| `batch` / `batch_line` | FK `procurement.*`, `PROTECT` | денормализация себестоимости |
| `quantity` | `Decimal(12,3)` | 1 для экземпляра; ≤ available для лота |
| `unit_cost_rub` | `Decimal(12,2)` editable=False | себестоимость за ед., **заморожена при проведении** |
| `total_cost_rub` | `Decimal(14,2)` editable=False | `money(unit_cost × quantity)` |
| `note` | `CharField` blank | примечание к позиции |
| `written_off_at` | `DateTimeField` null/blank | момент списания (= проведение документа) |
| `created_at` | auto | аудит |

**Ограничения БД:** `CheckConstraint` XOR(`part_item`,`stock_lot`) + `quantity > 0`.

### 5.2 Статусы `WriteOffDocument` — минимальный набор

```python
class Status(models.TextChoices):
    DRAFT     = "draft",     "Черновик"   # собираем позиции; склад НЕ трогаем
    COMPLETED = "completed", "Проведён"    # списание проведено (расход списан)
    CANCELED  = "canceled",  "Отменён"     # черновик отменён (склад не трогали)
```

**Рекомендация:** `draft → completed`; `draft → canceled` (отмена **черновика**).
Отмену **проведённого** списания (восстановление остатка) оставляем на будущий слой
корректировок — проведённый документ **immutable** (зеркально продажам/ремонту).

---

## 6. Причины списания (`Reason`) — минимальный набор

```python
class Reason(models.TextChoices):
    DAMAGED  = "damaged",  "Повреждение"
    LOST     = "lost",     "Потеря"
    DEFECT   = "defect",   "Брак"
    DISPOSAL = "disposal", "Утилизация"
    OBSOLETE = "obsolete", "Неликвид"
    OTHER    = "other",    "Прочее"
```

**Обоснование.** Набор покрывает реальные ситуации склада запчастей:
повреждение (`damaged`), потеря/недостача (`lost`), заводской брак (`defect`),
утилизация (`disposal`), неликвид/устаревшее (`obsolete`) и «прочее» (`other`) как
страховка. Это **классификация причины**, а не аналитика потерь (её не делаем). Без
`other` пользователь застрял бы на нетипичной причине; больше категорий — уже
аналитика (будущее).

---

## 7. Что можно списывать

| Объект | Как | Допустимый исходный статус |
|---|---|---|
| **`PartItem`** целиком (`quantity=1`) | скан/выбор экземпляра | **`available` или `quarantine`** и не зарезервирован (§12) |
| **`StockLot`** quantity (Decimal 12,3), частично/целиком | выбор лота + кол-во | **`available` или `quarantine`**; `qty ≤ lot.quantity − активный_резерв` |

**`PartType` без выбора item/lot не списываем** — это потребовало бы FIFO-автоподбора
(будущий слой). Пользователь выбирает **конкретный** экземпляр/лот.

> **Ключевое отличие от продажи/выдачи:** списание допускает **`quarantine`** как
> исходный статус (повреждённое/на проверке — главные кандидаты на списание), тогда
> как продажа/ремонт работают только с `available`. Это требует параметризации
> `_consume_*` (§14).

---

## 8. Влияние на `PartItem`

- При проведении экземпляр переходит в **`written_off`** (статус уже в enum, модель
  **не меняем**).
- Статус ставится **сервисом `inventory.write_off_part_item`**; ручная
  `ALLOWED_TRANSITIONS` не используется.
- `written_off` **не входит** в `ITEM_PHYSICAL_STATUSES` → экземпляр сразу выбывает
  из physical/available; после списания **недоступен** для продажи/резерва/ремонта/
  перемещения (все они требуют `available`).
- **`current_location` оставляем** как последнюю известную ячейку (аудит «откуда
  списан»); не очищаем — то же решение, что для sold/installed (плюс `from_location`
  в движении).

---

## 9. Влияние на `StockLot` — правило финального статуса

- `quantity` уменьшается на списанное количество **через сервис**.
- **Частичное списание разрешено**; лот **не дробим**.
- **При `quantity == 0` статус = `written_off`** (а **не** `depleted`).
- Частичное списание статус **не меняет** (лот остаётся `available`/`quarantine`).

**Обоснование правила (рекомендация заказчика).** `depleted` означает «лот
**исчерпан обычным оборотом**» (продажа/выдача/корректировка), `written_off` —
«лот **списан как потеря по документу**». Семантическое различие важно для аудита
(почему лот на нуле). **Физический баланс не страдает:** и `written_off`, и
`depleted` **вне** `LOT_PHYSICAL_STATUSES` → `_compute_balance` даёт 0 в обоих
случаях. То есть выбор статуса — чисто документарный, на корректность кэша не влияет.

**Техническая реализация:** параметр `zero_status` у `_consume_stock_lot` (§14):
продажа/выдача → `DEPLETED` (по умолчанию), списание → `WRITTEN_OFF`.

---

## 10. `StockMovement`

| Поле движения | Значение |
|---|---|
| `movement_type` | **`WRITE_OFF_ITEM`** / **`WRITE_OFF_LOT`** |
| `from_location` | текущая ячейка (`item.current_location` / `lot.location`) |
| `to_location` | **`null`** (выбытие со склада) |
| `quantity` | списанное количество (1 / qty) |
| `unit_cost_rub` | `landed_cost_rub` / `landed_unit_cost_rub` |
| `total_cost_rub` | `unit_cost × quantity` (в `StockMovement.save()`) |
| `document_type` | **`"write_off"`** |
| `document_id` | `WriteOffDocument.id` |

**Отдельные типы `WRITE_OFF_*` (рекомендация), а не `adjust_out`.** Обоснование:
`adjust_out` — это **техническая корректировка остатка** (будущая инвентаризация/
сверка), а списание — **осознанный бизнес-документ с причиной**. Отдельный тип
движения позволяет в журнале/отчётах отличать «списали как брак/потерю» от
«скорректировали остаток», не разбирая `document_type`. `adjust_out` оставляем под
будущие корректировки/инвентаризацию (§21). Альтернатива (переиспользовать
`adjust_out`) смешала бы потери и техкорректировки — в открытых вопросах.

**Минимальная правка инвентаря:** `MovementType += WRITE_OFF_ITEM, WRITE_OFF_LOT` →
миграция `inventory/0008_alter_stockmovement_movement_type.py` (только `AlterField`).

---

## 11. `StockBalance`

- Остаётся **кэшем** (не источник истины).
- `quantity_physical` **уменьшается** (экземпляр `written_off` исключён / `lot.quantity`
  упало) через `inventory.write_off_*` → `_refresh_balance`.
- `quantity_available`/`quantity_quarantine` уменьшаются **в зависимости от исходного
  статуса** объекта (списали `available` → падает available; списали `quarantine` →
  падает quarantine; `physical` падает в обоих).
- `quantity_reserved` напрямую не меняется, но **нельзя списывать зарезервированное**
  (§12), поэтому `available` не уходит в минус.

---

## 12. Резервы

- **Нельзя списать `PartItem`, находящийся в активном `Reservation`.**
- **Нельзя списать количество из `StockLot` так, чтобы `available` ушёл ниже 0** из-за
  активного резерва: `qty ≤ lot.quantity − active_reserved_for_lot(lot)`.
- Проверки — через **public** `apps.sales.is_part_item_reserved` /
  `active_reserved_for_lot` (как в Слоях 17/18); зависимость `writeoffs → sales`
  ацикл��чна.
- **Автоматическую отмену резерва НЕ делаем** — резерв должен быть отменён отдельно
  **до** списания (осознанное действие, без скрытого освобождения брони).

> Примечание: `quarantine`-объекты не могут быть активно зарезервированы (бронь
> требует `available`, Слой 15), поэтому для них резерв-проверка — no-op; для
> `available`-объектов проверка реальная.

---

## 13. Себестоимость

- `unit_cost_rub` **фиксируется** в `WriteOffLine` при проведении (из
  `landed_cost_rub`/`landed_unit_cost_rub`); `total_cost_rub` тоже.
- `WriteOffDocument.cost_total` — заморожен при проведении (**сумма потерь по
  себестоимости**).
- Будущие изменения landed cost историю списания не двигают (как в продажах/ремонте/
  возвратах).
- Себестоимость видна **только при `can_view_purchase_cost`** (контекст `show_costs`).
- **Полноценную аналитику потерь не делаем** — только фиксация себестоимости в строке.

---

## 14. Сервисы `apps/inventory` — параметризация общего helper

Списание — это тот же складской **расход**, что продажа/выдача, но с тремя отличиями:
**(а)** допускает исходный `quarantine`, **(б)** лот при нуле → `written_off`,
**(в)** `document_type="write_off"`, тип `WRITE_OFF_*`, статус `written_off`. Чтобы не
дублировать и **не сломать продажи/ремонт**, **параметризуем** существующие
`_consume_*` двумя новыми kwargs (с дефолтами = текущее поведение):

```python
# _consume_part_item: + allowed_statuses=(AVAILABLE,)   (дефолт = текущее)
#   проверка: if item.status not in allowed_statuses: raise InventoryError(unavailable_msg)
#
# _consume_stock_lot:  + allowed_statuses=(AVAILABLE,), zero_status=DEPLETED  (дефолты = текущее)
#   проверка: if lot.status not in allowed_statuses: raise ...
#   при нуле: lot.status = zero_status

def write_off_part_item(item, *, by=None, document_id=None, comment=""):
    return _consume_part_item(
        item, new_status=PartItem.Status.WRITTEN_OFF,
        movement_type=StockMovement.MovementType.WRITE_OFF_ITEM, document_type="write_off",
        allowed_statuses=(PartItem.Status.AVAILABLE, PartItem.Status.QUARANTINE),
        unavailable_msg="Списать можно только доступный или карантинный экземпляр.",
        by=by, document_id=document_id, comment=comment)

def write_off_stock_lot_quantity(lot, quantity, *, by=None, document_id=None, comment=""):
    return _consume_stock_lot(
        lot, quantity, movement_type=StockMovement.MovementType.WRITE_OFF_LOT,
        document_type="write_off",
        allowed_statuses=(StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE),
        zero_status=StockLot.Status.WRITTEN_OFF,
        positive_msg="Количество списания должно быть больше нуля.",
        unavailable_msg="Списать можно только доступный или карантинный лот.",
        over_msg="Нельзя списать {quantity}: в лоте {in_lot}.",
        by=by, document_id=document_id, comment=comment)
```

- **Безопасность рефактора:** `sell_*`/`issue_*` используют **дефолты** новых kwargs
  (`allowed_statuses=(AVAILABLE,)`, `zero_status=DEPLETED`) → их поведение **не
  меняется**; гарантируют **319 существующих тестов** (вкл. `test_sales`,
  `test_repairs`). Нулевой риск — приемлемая альтернатива: отдельные `write_off_*`
  без рефактора `_consume_*` (дублирование) — открытый вопрос; рекомендация — параметры.
- `write_off_*` работают в `transaction.atomic`, под `select_for_update`, меняют
  physical, создают `WRITE_OFF_*`, обновляют `StockBalance`, **не знают** о резервах
  и о `apps/writeoffs` (резерв-проверки делает writeoffs до вызова).

---

## 15. Сервисы `apps/writeoffs/services.py`

```python
class WriteOffError(Exception): ...

create_write_off(*, reason, comment="", by) -> WriteOffDocument           # draft
add_part_item_to_write_off(doc, item, *, note="", by) -> WriteOffLine
add_stock_lot_to_write_off(doc, lot, quantity, *, note="", by) -> WriteOffLine
remove_write_off_line(line, *, by)
complete_write_off(doc, *, by) -> WriteOffDocument        # draft → completed (выбытие)
cancel_write_off(doc, *, by)                              # draft → canceled
calculate_write_off_costs(doc) -> Decimal                # сумма из замороженных строк
```

**`complete_write_off` (оркестрация, `@transaction.atomic`):**
1. lock документ; должен быть `draft`; строк ≥ 1 (иначе `WriteOffError`).
2. по каждой строке (под `select_for_update` объекта):
   - доступность к списанию: экземпляр в `{available, quarantine}` **и**
     `not sales.is_part_item_reserved(item)`; лот в `{available, quarantine}` и
     `qty ≤ lot.quantity − sales.active_reserved_for_lot(lot)`;
   - **заморозка себестоимости** (`unit_cost_rub`/`total_cost_rub` из landed);
   - физическое выбытие через `inventory.write_off_part_item` /
     `write_off_stock_lot_quantity` (`document_id=doc.pk`); `written_off_at=now`.
3. `cost_total` = сумма строк; `status=completed`, `completed_at=now`.

- Все действия — **только через сервисы**; вьюхи ledger не пишут (тест §23).
- `add_*` валидируют статус-источник и резервы на этапе добавления (повтор — при
  проведении под блокировкой); `quantity` лота: `0 < qty ≤ available − уже_в_документе`.

---

## 16. Инварианты (и кто гарантирует)

| Инвариант | Гарант |
|---|---|
| Нельзя провести пустое списание | `complete_write_off`: строк ≥ 1 |
| Нельзя списать `receiving` | `write_off_*`: статус ∈ {available, quarantine} |
| Нельзя списать `sold`/`installed`/`written_off`/`depleted` | `write_off_*`: статус ∈ {available, quarantine} |
| Нельзя списать `PartItem` в активном резерве | `writeoffs`: `sales.is_part_item_reserved(item)` |
| Нельзя списать `StockLot` больше available (с учётом резерва) | `write_off_*`: `qty ≤ lot.quantity`; `writeoffs`: минус `active_reserved_for_lot` |
| Нельзя списать `quantity ≤ 0` | `write_off_*`: `quantity > 0` |
| Нельзя провести документ дважды | `complete_write_off` требует `draft` |
| Проведённое списание **immutable** | сервисы: мутации только при `draft` |
| Списание **создаёт** `StockMovement` (`WRITE_OFF_*`) | `inventory.write_off_*` |
| Списание **уменьшает** physical (`status`/`quantity`) | `inventory.write_off_*` |
| Списание **фиксирует** себестоимость | `complete_write_off` (заморозка строки) |
| Списание **не** создаёт `Sale`/`RepairOrder`/`StockReturn` | границы (нет создания этих документов) |
| Списание **не** создаёт оплату/чек/refund | границы (нет платёжных полей/логики) |

---

## 17. Транзакции и блокировки

- `complete_write_off` — целиком в `transaction.atomic`.
- `select_for_update` на `WriteOffDocument` и на каждом `PartItem`/`StockLot`.
- **Защита от двойного списания:** `write_off_part_item` требует статус ∈ {available,
  quarantine} под блокировкой (после списания — `written_off`); `complete_write_off`
  требует `draft`.
- **Защита от ухода `available`/`quantity` в минус** — проверки §16 под блокировкой.
- Последовательные тесты на отсутствие минуса; конкурентный Postgres-тест — будущий
  слой (тестовый стек SQLite).

---

## 18. Права

Новая возможность **`MANAGE_WRITE_OFFS`** (`roles.py`):

| Роль | Создавать/проводить | Видеть списания | Себестоимость |
|---|---|---|---|
| Администратор | ✅ | ✅ | ✅ |
| Руководитель | ✅ | ✅ | ✅ |
| **Кладовщик** | ✅ | ✅ | нет |
| Продавец/Мастер | ❌ | ✅ | нет |
| Наблюдатель | ❌ | ✅ | по `can_view_purchase_cost` |

**Обоснование.** Списание — **складское** действие (кладовщик фиксирует брак/потерю
на складе), поэтому право даём **кладовщику** (а также Админу/Руководителю).
**Продавцу/Мастеру право НЕ даём** (списание — чувствительное выбытие/потеря, не его
зона; но **видеть** документы он может — полезно для прозрачности). Наблюдатель —
только просмотр. Себестоимость потерь — под `can_view_purchase_cost`.

- `roles.py`: `MANAGE_WRITE_OFFS` для `ADMIN`/`MANAGER`/`STOREKEEPER`.
- `accounts/models.py`: `can_manage_write_offs`. Без миграции (возможности — код).
- Просмотр — `login_required`; мутации — под `manage_write_offs`.

---

## 19. UI (`apps/writeoffs`, шаблоны `templates/writeoffs/`)

| Экран | URL (`name`) | Право |
|---|---|---|
| Список списаний | `/write-offs/` (`write_off_list`) | просмотр — вошедшие |
| Карточка списания | `…/<pk>/` (`write_off_detail`) | просмотр — вошедшие |
| Создать списание | `…/new/` (`write_off_create`) | `manage_write_offs` |
| Добавить `PartItem` | POST `…/<pk>/add-item/` | `manage_write_offs` |
| Добавить `StockLot` qty | POST `…/<pk>/add-lot/` | `manage_write_offs` |
| Снять позицию | POST `…/lines/<pk>/remove/` | `manage_write_offs` |
| Провести (списать) | POST `…/<pk>/complete/` | `manage_write_offs` |
| Отменить (черновик) | POST `…/<pk>/cancel/` | `manage_write_offs` |

- Создание: выбор **причины** (`reason`) и комментария; затем добавление позиций
  (экземпляр по коду / количество из лота — как в карточке ремонта Слоя 17).
- Карточка: причина, **списанные позиции** (деталь, экземпляр/лот, кол-во);
  **себестоимость строк и `cost_total` — только `can_view_purchase_cost`**.
- Проведённый документ — без кнопок правки (immutable).
- No-JS: server-rendered формы.

---

## 20. Интеграция с `/search/` и карточками

- Действие **«Списать»** в `/search/` — только для ролей с `manage_write_offs`
  (рядом с «Продать»/«Выдать в ремонт»), ведёт на создание документа списания.
- (Опц.) ссылка **«Списать»** из карточек `item_detail`/`lot_detail` для
  `manage_write_offs` — удобно при работе с конкретным экземпляром/лотом.
- После проведения `/search/` показывает **уменьшенный остаток** (кэш пересобран
  сервисом).
- **Не делаем** быстрый одно-кликовый checkout (списание — через документ с причиной).

---

## 21. Чем списание отличается от инвентаризации

- **Списание** — осознанный документ по **конкретной причине** (брак/потеря/…),
  уменьшает остаток через `WRITE_OFF_*`.
- **Инвентаризация/корректировка остатков** (сверка факта с системой, ±дельты) —
  **будущий слой**; она пойдёт через `adjust_in`/`adjust_out`.
- Поэтому `adjust_out` **не используем** как пользовательский документ списания —
  вводим отдельные `WRITE_OFF_*`. Так в журнале «потеря по документу» и «техническая
  корректировка» различимы по типу движения, а не только по `document_type`.

---

## 22. Management-команды

- **Не требуются.** Себестоимость замораживается при проведении; баланс
  пересобирается существующими `rebuild_stock_balance`/`check_stock_balance` из
  первички (списание уже отражено в `StockLot.quantity`/`PartItem.status`).
- Опционально (только при необходимости) — `check_write_off_quantity` для сверки
  целостности. По умолчанию **нет**.

---

## 23. Тесты (`tests/test_writeoffs.py`)

1. Можно создать черновик списания (`draft`).
2. Нельзя провести пустое списание (`WriteOffError`).
3. Можно добавить `PartItem` в документ.
4. `complete_write_off` списывает `available` `PartItem`.
5. `PartItem` становится `written_off`.
6. Создаётся `StockMovement` `WRITE_OFF_ITEM` (`from`=ячейка, `to`=null,
   `document_type="write_off"`, `document_id=doc.id`, qty=1).
7. `StockBalance` уменьшается.
8. `WriteOffLine` фиксирует себестоимость (`unit_cost_rub`/`total_cost_rub`).
9. Можно списать **`quarantine`** `PartItem` (специфика слоя).
10. Можно списать `StockLot` quantity (частично) → `quantity↓`.
11. `StockLot`, списанный **полностью**, получает статус **`written_off`** (не depleted);
    движение `WRITE_OFF_LOT`.
12. Нельзя списать `StockLot` больше available.
13. Нельзя списать `receiving`.
14. Нельзя списать зарезервированное (active `Reservation`).
15. Нельзя списать `sold`/`installed`/`written_off`/`depleted`.
16. Нельзя списать `quantity ≤ 0`.
17. Нельзя провести документ дважды (`completed` immutable; `remove`/`cancel` тоже).
18. **Архитектурный мок:** при проведении вьюха вызывает сервис и сама
    `StockMovement`/`StockBalance` не пишет (`patch` сервиса → ledger неизменен).
19. Hidden/query-параметры перепроверяются сервером (подмена документа/item/lot/qty →
    ошибка/404, без эффекта).
20. Списание **не создаёт** `Sale` / `RepairOrder` / `StockReturn`.
21. Списание **не создаёт** оплату/чек/refund (нет платёжных полей).
22. Кладовщик/админ с правом могут провести; Продавец/Мастер — **403** (но видит список).
23. Себестоимость скрыта без `can_view_purchase_cost`.
24. Регресс: продажи/ремонт (`test_sales`/`test_repairs`) зелёные после параметризации
    `_consume_*`.

---

## 24. Ручная проверка

1. Кладовщиком → `/search/` найти деталь → «Списать» → создать документ (причина
   «Брак», комментарий).
2. Добавить `available`-экземпляр и количество из лота → провести.
3. В «Движениях» — `WRITE_OFF_ITEM`/`WRITE_OFF_LOT` с `document=write_off`, `from`→`—`;
   в `/search/` «доступно»/«физически» уменьшилось; экземпляр — `written_off`, лот —
   меньше / `written_off` при нуле.
4. Списать **карантинный** экземпляр → успех (специфика слоя).
5. Себестоимость строк/`cost_total`: кладовщику не видна; админу — видна.
6. Попробовать списать тот же экземпляр снова → отказ (уже `written_off`).
7. Попробовать списать зарезервированную деталь → отказ.
8. Продавцом/Мастером → действие списания недоступно (403), список виден.

---

## 25. Критерии готовности

1. Списание `PartItem`/`StockLot` идёт **только через сервисы**: документ —
   `writeoffs`, физика/ledger — `inventory.write_off_*`; вьюха ledger не пишет
   (мок-тест §23.18).
2. `PartItem` → `written_off` и недоступен; `StockLot.quantity` падает, **`written_off`
   при 0** (не depleted); создаётся `WRITE_OFF_ITEM`/`WRITE_OFF_LOT` с
   `document=write_off`; баланс пересобран.
3. Списание допускает `available` **и** `quarantine`; `receiving`/`sold`/`installed`/
   `written_off`/`depleted`/зарезервированное — нельзя.
4. Себестоимость заморожена в строке; `cost_total` на документе; ретро-пересчёта нет.
5. Права: `MANAGE_WRITE_OFFS` (Админ/Руководитель/Кладовщик); Продавец/Мастер не
   проводит; себестоимость — под `can_view_purchase_cost`.
6. Границы: нет продажи/ремонта/возврата/оплаты/кассы/чека/refund/гарантии/
   инвентаризации/аналитики/PDF/проводок; `adjust_out` не используется как списание;
   `StockBalance` не источник истины; `StockMovement` из вьюх не пишется.
7. Тесты зелёные (вкл. регресс продаж/ремонта); `ruff`/`djlint` чисты;
   `manage.py check` ок; `makemigrations --check` — миграции **только** `apps/writeoffs`
   (+ seed) и **одна** `apps/inventory` (типы `WRITE_OFF_*`).

---

## 26. Файлы (создаются/изменяются)

**Изменяются — `apps/inventory`:**
- `models.py` — `MovementType += WRITE_OFF_ITEM, WRITE_OFF_LOT`.
- `migrations/0008_alter_stockmovement_movement_type.py`.
- `services.py` — параметры `allowed_statuses` (оба `_consume_*`) и `zero_status`
  (`_consume_stock_lot`); новые `write_off_part_item`/`write_off_stock_lot_quantity`.

**Создаются — `apps/writeoffs/`:**
- `__init__.py`, `apps.py`, `models.py` (`WriteOffDocument`, `WriteOffLine`),
  `services.py`, `forms.py`, `views.py`, `urls.py`, `admin.py`.
- `migrations/__init__.py`, `migrations/0001_initial.py`,
  `migrations/0002_seed_writeoff_sequence.py` (ключ `write_off`, `WO-`).

**Изменяются — `apps/accounts`:**
- `roles.py` — `MANAGE_WRITE_OFFS` + привязка ролей (Админ/Руководитель/Кладовщик).
- `models.py` — `can_manage_write_offs`.
- `context_processors.py` — пункт «Списания».

**Изменяются — прочее:**
- `config/settings/base.py` — `LOCAL_APPS += "apps.writeoffs"`.
- `config/urls.py` — `path("write-offs/", include("apps.writeoffs.urls"))`.
- `apps/core/views.py` + `templates/core/search.html` — действие «Списать».

**Создаются — шаблоны `templates/writeoffs/`:**
- `write_off_list.html`, `write_off_detail.html`, `write_off_form.html`.

**Тесты:** `tests/test_writeoffs.py`.

**Без изменений:** `PartItem.Status.WRITTEN_OFF` / `StockLot.Status.WRITTEN_OFF`
(уже есть → без модельных правок этих моделей).

---

## 27. Что будет закоммичено

Два коммита (как в Слоях 5–18):
1. `План Слоя 19: списания` — этот файл (push в `origin/main`).
2. `Слой 19: списания` — реализация (после `pytest`, `ruff`, `djlint`,
   `makemigrations --check`, `manage.py check`), затем **push в `origin/main`**.

Останавливаемся перед **Слоем 20**.

---

## Границы Слоя 19 (чего НЕ делаем)

- Не реализуем продажу, ремонт, возврат, оплату, кассу, чеки, refund, гарантию,
  инвентаризацию, полноценную аналитику потерь, бухгалтерские проводки, PDF, CRM.
- **Не пишем `StockMovement`/`StockBalance`/`PartItem.status`/`StockLot.quantity`
  напрямую из `apps/writeoffs`** — только через `inventory.write_off_*`.
- Не используем `adjust_out` как пользовательский документ списания.
- Не превращаем `StockBalance` в источник истины.

---

## Решения (утверждены 2026-06-26)

Все рекомендации приняты заказчиком. Вопросы закрыты:

1. **Имя приложения:** `apps/writeoffs` — ✅ принято (документ отдельно, не ручная
   правка склада и не часть продаж/ремонта/возвратов).
2. **Тип движения:** `WRITE_OFF_ITEM`/`WRITE_OFF_LOT` — ✅ принято; `adjust_out`
   оставляем под будущую инвентаризацию/техкорректировки.
3. **Финальный статус лота:** при полном списании — `written_off`; `depleted` —
   только под обычное исчерпание продажей/ремонтом — ✅ принято.
4. **Исходные статусы:** списываем `available` **и** `quarantine` (карантин — главный
   кандидат для брака/повреждения/утилизации) — ✅ принято.
5. **Helper в inventory:** параметризуем `_consume_*` (`allowed_statuses`,
   `zero_status`; дефолты сохраняют sell/issue, под защитой 319 тестов) — ✅ принято
   при условии: продажи/ремонт/возвраты зелёные; если рефактор расползается —
   отдельные `write_off_*` с минимальным дублированием.
6. **Причины:** `damaged/lost/defect/disposal/obsolete/other` — ✅ принято.
7. **Право:** `MANAGE_WRITE_OFFS` для Админ/Руководитель/Кладовщик; Продавец/Мастер —
   только просмотр; Наблюдатель — просмотр; себестоимость под `can_view_purchase_cost`
   — ✅ принято.
8. **Резервы:** запрет списания зарезервированного, без авто-отмены брони (резерв
   снимают отдельно до списания) — ✅ принято.
9. **Статусы:** `draft`/`completed` + отмена **черновика** (`canceled`); отмена
   проведённого — будущий слой корректировок — ✅ принято.
10. **Номер:** `WO-000001` (ASCII) — ✅ принято.
11. **Причина на документе** (одна на документ) — ✅ принято.
