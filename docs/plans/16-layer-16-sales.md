# План реализации — Слой 16. Продажи (коммерческий документ + складской расход)

**Статус:** на согласовании (2026-06-25) · **Код не пишется до утверждения.**

---

## 1. Цель слоя

Сделать **продажу детали как коммерческий документ**, который впервые в проекте
**порождает физический складской расход**:

- фиксирует клиента/комментарий и **строки продажи**;
- **списывает доступный остаток** (PartItem целиком / StockLot quantity частично);
- **создаёт `StockMovement` расхода**;
- **фиксирует выручку, себестоимость и прибыль** в момент продажи (заморожено);
- умеет **конвертировать активный резерв** в продажу;
- НЕ реализует оплату, кассу, чеки, возвраты, аналитику как отдельные подсистемы.

**Главный архитектурный контроль (как в Слоях 10/12/14/15):** физический расход и
запись в ledger идут **только через сервисы `apps/inventory`** (новые
`sell_part_item` / `sell_stock_lot`). **`apps/sales` коммерческий документ ведёт
сам, но физику/ledger НЕ трогает напрямую** — ни `StockMovement.objects.create`,
ни записи `PartItem.status`/`StockLot.quantity`/`StockBalance` из sales. Граница
закрепляется тестом-моком (§21).

**Разделение ответственности (ключевая мысль слоя):**

| Слой | Отвечает за |
|---|---|
| `apps/sales` (Sale/SaleLine) | коммерческий документ: цены, выручка, себестоимость, прибыль, **проверка резервов**, оркестрация |
| `apps/inventory` (`sell_*`) | физический расход: статус/количество, `StockMovement`, пересчёт `StockBalance` — **ledger-истина**, не знает о резервах |

Резерв-осведомлённость живёт **только в `apps/sales`** (он знает `reserved_for` /
`_active_reserved_for_lot` из Слоя 15); `inventory.sell_*` — резерв-агностичны и
переиспользуемы будущими слоями (списание/установка).

### Что уже есть (переиспользуем)

| Уже реализовано | Где | Роль в Слое 16 |
|---|---|---|
| `PartItem.Status.SOLD = "sold"` | `inventory/models.py` | целевой статус проданного экземпляра (модель **не меняем**) |
| `StockLot.Status.DEPLETED` + обнуление в `adjust_stock_lot_quantity` | `inventory` | статус лота при `quantity == 0` |
| `StockMovement.document_type` / `document_id` (пустые) | `inventory/models.py` | ссылка движения на `Sale` (комментарий в коде: «навесится при появлении Sale… Слои 16–19») |
| `StockMovement` append-only + XOR(item,lot) + `quantity>0` | `inventory` | расходное движение продажи |
| `_record_movement`, `_refresh_balance`, `ITEM_PHYSICAL_STATUSES` (sold НЕ входит) | `inventory/services.py` | расход уменьшает physical автоматически |
| `Reservation`/`ReservationLine`, `reserved_for`, `_active_reserved_for_lot`, `_item_actively_reserved(exclude=)` | `apps/sales` (Слой 15) | конверсия резерва, проверка «не зарезервировано другим» |
| `PartType.recommended_price` / `min_price`; `money()` | `catalog` / `procurement` | подстановка/подсказка цены, округление |
| `NumberSequence.next(key)` | `inventory` | номер продажи `ПРОД-…` |
| Возможности через `roles.py` (код, без миграций) | `accounts` | новая `MANAGE_SALES` |

### Что нового в Слое 16

- Модели **`Sale`**, **`SaleLine`** в `apps/sales`.
- Новые типы движения **`SALE_ITEM` / `SALE_LOT`** (`inventory.MovementType`) →
  **миграция инвентаря** (`AlterField` choices) — единственная модельная правка
  инвентаря этого слоя.
- Сервисы `sell_part_item` / `sell_stock_lot` в `apps/inventory` (расход + ledger).
- Сервисы продаж в `apps/sales` (документ, цены, totals, конверсия резерва).
- Возможность **`MANAGE_SALES`**, экраны продаж, действие «Продать» в `/search/`.

**Чего слой НЕ делает:** оплата, касса, чеки, возвраты, установки, гарантия,
списания вне продажи, инвентаризация, аналитика продаж, PDF, CRM, FIFO-автоподбор
`PartType`; не превращает `StockBalance` в источник истины.

---

## 2. Где размещаем продажи

**Используем существующий `apps/sales`** (создан в Слое 15). Новые модели —
`Sale`, `SaleLine`. Обоснование: продажа — естественное продолжение брони (та же
доменная зона «коммерция»), `Sale.reservation` ссылается на `Reservation`,
направление зависимостей `sales → inventory` уже выстроено. Новый app не нужен.

---

## 3. Модели

### 3.1 `Sale` (шапка продажи)

| Поле | Тип | Назначение |
|---|---|---|
| `number` | `CharField` unique, editable=False (`ПРОД-000001`) | человекочитаемый номер |
| `status` | `CharField(choices=Status)` | `draft`/`completed` (+ future `canceled`/`voided`, §5) |
| `customer_name` | `CharField` | клиент (обязателен) |
| `customer_phone` | `CharField` blank | контакт |
| `comment` | `CharField` blank | примечание |
| `reservation` | FK `Reservation`, `SET_NULL`, null/blank, related_name="sales" | источник-резерв (если продажа из брони) |
| `sold_by` | FK user, `SET_NULL` | кто провёл |
| `sold_at` | `DateTimeField` null/blank | момент завершения (ставится в `complete_sale`) |
| `created_at` / `updated_at` | auto | аудит |
| `canceled_at` | `DateTimeField` null/blank | future (отмена/сторно — слой возвратов) |
| `revenue_total` | `Decimal(14,2)` default 0 | выручка (заморожена при завершении) |
| `cost_total` | `Decimal(14,2)` default 0 | себестоимость (заморожена) |
| `profit_total` | `Decimal(14,2)` default 0 | прибыль = revenue − cost (заморожена) |

### 3.2 `SaleLine` (строка продажи)

| Поле | Тип | Назначение |
|---|---|---|
| `sale` | FK `Sale`, `CASCADE`, related_name="lines" | шапка |
| `part_type` | FK `catalog.PartType`, `PROTECT` | денормализация |
| `part_item` | FK `inventory.PartItem`, `PROTECT`, null | поштучно (XOR) |
| `stock_lot` | FK `inventory.StockLot`, `PROTECT`, null | количественно (XOR) |
| `batch` / `batch_line` | FK `procurement.*`, `PROTECT` | денормализация для себестоимости/аналитики |
| `quantity` | `Decimal(12,3)` | 1 для экземпляра; дробное для лота |
| `unit_price` | `Decimal(12,2)` | цена продажи за ед. (вводит продавец) |
| `total_price` | `Decimal(14,2)` | = `money(unit_price × quantity)` |
| `unit_cost_rub` | `Decimal(12,2)` editable=False | себестоимость за ед., **заморожена в момент продажи** |
| `total_cost_rub` | `Decimal(14,2)` editable=False | = `money(unit_cost × quantity)` |
| `profit_rub` | `Decimal(14,2)` editable=False | = `total_price − total_cost_rub` |

**Ограничения БД (как у `StockMovement`/`ReservationLine`):** `CheckConstraint`
XOR(`part_item`, `stock_lot`) + `CheckConstraint` `quantity > 0`.

---

## 4. Нумерация

- **Отдельный ключ `NumberSequence` для продаж**, seed-миграция в `apps/sales`
  (как `reservation` в Слое 15).
- Формат **`S-000001`** (prefix `S-`) — **зафиксировано заказчиком**. В UI
  заголовок — «Продажа S-000001».

**Обоснование формата:** короткий ASCII-формат удобен для ссылок, поиска, логов и
будущих документов (не требует кириллицы/раскладки). Русское название несёт
UI-подпись «Продажа …». (Кириллический `ПРОД-…` единообразен с `П-`/`РЕЗ-`, но
проиграл по удобству ссылок/поиска.)

---

## 5. Статусы `Sale`

```python
class Status(models.TextChoices):
    DRAFT     = "draft",     "Черновик"
    COMPLETED = "completed", "Проведена"
    # FUTURE (слой возвратов/сторно): определены, в Слое 16 не выставляются.
    CANCELED  = "canceled",  "Отменена"
    VOIDED    = "voided",    "Сторнирована"
```

**Рекомендация — в Слое 16 только `draft → completed`.** Отмена проведённой
продажи — это **складское сторно** (вернуть остаток, обратить движение, обратить
резерв-конверсию): это слой возвратов/сторно. Делать это сейчас усложнило бы слой
и потребовало бы reverse-движений. Поэтому `canceled`/`voided` **закладываем в
choices** (чтобы не плодить миграцию позже), но **не реализуем** переходы.
Проведённая продажа **immutable** (§19).

---

## 6. Что можно продавать

| Объект | Как | Доступность проверяет |
|---|---|---|
| **`PartItem`** целиком (`quantity=1`) | прямо из доступного / из резерва | `available` и не зарезервирован другим (§11/§19) |
| **`StockLot`** quantity (Decimal 12,3), частично/целиком | прямо / из резерва | `qty ≤ lot.quantity − резерв_других` |
| **из активного `Reservation`** | `create_sale_from_reservation` | резерв `active`, не больше зарезервированного |
| **напрямую без резерва** | `create_sale` + добавление строк | доступность с учётом чужих резервов |

**`PartType` без выбора item/lot не продаём** — это потребовало бы стратегии
автоподбора лотов (FIFO/FEFO) и распределения; вынесено в будущий слой. На Слое 16
продавец **выбирает конкретный** экземпляр/лот (скан/список), как в резервах.

---

## 7. Как продажа влияет на `PartItem`

- При завершении продажи экземпляр переходит в **`sold`** (статус уже есть в enum
  — модель **не меняем**).
- Статус ставится **сервисом `inventory.sell_part_item`** напрямую (как Слой 15 не
  ставил `reserved`, так Слой 16 — **производитель** `sold`); ручная
  `ALLOWED_TRANSITIONS` не используется (она для UI-переходов кладовщика).
- `sold` **не входит** в `ITEM_PHYSICAL_STATUSES` → экземпляр сразу перестаёт
  считаться `physical`/`available` (главное требование «после продажи не доступен»).

**`current_location` — оставляем как последнюю ячейку (рекомендация),** не очищаем.
Обоснование: доступность определяется **статусом**, а не ячейкой (`sold` уже
исключён из physical-пар, см. `_primary_pairs`), поэтому хранение ячейки не влияет
на баланс; зато сохраняется история «откуда продано» (плюс `from_location` в
движении). Очистка ячейки потеряла бы аудит без выгоды.

---

## 8. Как продажа влияет на `StockLot`

- `quantity` уменьшается на проданное количество **через сервис**
  `inventory.sell_stock_lot` (не напрямую из sales).
- При `quantity == 0` → статус **`depleted`** (как в `adjust_stock_lot_quantity`).
- **Частичная продажа разрешена** — это обычный количественный товар (лот не
  дробится, просто уменьшается остаток).
- Создаётся `StockMovement` типа **`SALE_LOT`** (§9).

---

## 9. `StockMovement` (расходное движение продажи)

| Поле движения | Значение |
|---|---|
| `movement_type` | **`SALE_ITEM`** (экземпляр) / **`SALE_LOT`** (лот) |
| `from_location` | ячейка товара (`item.current_location` / `lot.location`) |
| `to_location` | **`null`** (расход «наружу») |
| `quantity` | проданное количество (1 / qty лота) |
| `unit_cost_rub` | `PartItem.landed_cost_rub` / `StockLot.landed_unit_cost_rub` |
| `total_cost_rub` | `unit_cost × quantity` (считается в `StockMovement.save()`) |
| `document_type` | **`"sale"`** |
| `document_id` | `Sale.id` |

**Название типа — `SALE_ITEM` / `SALE_LOT` (рекомендация).** Обоснование:
повторяет сложившийся **парный** паттерн `RECEIVE_ITEM/RECEIVE_LOT`,
`MOVE_ITEM/MOVE_LOT`. Будущие расходы (списание/установка) добавят свои типы
(`WRITE_OFF_*`, `INSTALL_*`), а `document_type` различает источник. Альтернатива —
единый `SALE_OUT`/`ISSUE` для обоих — рвёт парность (в открытых вопросах).

**Минимальная правка инвентаря:** добавить `SALE_ITEM`/`SALE_LOT` в
`MovementType` → миграция `inventory/0005_alter_stockmovement_movement_type`
(только `AlterField` choices, данные не трогает). Плюс расширить `_record_movement`
параметрами `document_type=""` / `document_id=None` (поля **уже существуют** →
без миграции).

`from`/`to`: текущие `CheckConstraint` требуют только XOR(item,lot) и `quantity>0`
— расход с `to_location=null` допустим без правок ограничений.

---

## 10. `StockBalance`

- Обновляется **через инвентарь-сервисы** (`sell_*` вызывают `_refresh_balance`),
  остаётся **кэшем** (не источник истины).
- После продажи: `quantity_physical` уменьшается (экземпляр `sold` исключён /
  `lot.quantity` упало); если продажа из резерва — `quantity_reserved` уменьшается
  (резерв стал `converted_to_sale`, провайдер Слоя 15 его больше не считает);
  `quantity_available` пересчитывается формулой `physical − quarantine − reserved`.
- При обнулении строки (физического остатка не осталось) строка кэша удаляется
  (`_refresh_balance` уже так делает).
- **Не превращаем `StockBalance` в источник истины** — первичка остаётся
  `PartItem`/`StockLot` (+ `ReservationLine`).

---

## 11. Резервы ↔ продажи

**Продажа из активного `Reservation`** (`create_sale_from_reservation` → затем
`complete_sale`):

1. создаётся `Sale` со ссылкой `reservation`;
2. `SaleLine` копируются из `ReservationLine` (тот же `part_item`/`stock_lot` +
   `quantity`); `unit_price` подставляется из `recommended_price` (продавец правит);
3. при `complete_sale` каждая строка **списывает** соответствующий
   `PartItem`/`StockLot` через `inventory.sell_*`;
4. `Reservation` переводится в **`converted_to_sale`**;
5. `reserved` **освобождается через пересчёт** (провайдер считает только `active`);
6. **нельзя продать больше, чем зарезервировано** (qty строки ≤ зарезервированного).

Проверка доступности при продаже **из резерва** исключает собственный резерв:
- экземпляр: `_item_actively_reserved(item, exclude=reservation)` должно быть
  `False` (т.е. чужих активных броней нет);
- лот: `qty ≤ lot.quantity − (активный_резерв − свой_резерв)` (расширим
  `_active_reserved_for_lot` параметром `exclude`).

**Прямая продажа без резерва** проверяет доступность с учётом **чужих** резервов:
- экземпляр: `available` **и** `not _item_actively_reserved(item)`;
- лот: `qty ≤ lot.quantity − _active_reserved_for_lot(lot)`.

Конвертируем **весь** резерв (все строки → строки продажи); частичная конверсия —
будущее (открытый вопрос).

---

## 12. Сервисы

**`apps/sales/services.py`** (коммерческий документ, оркестрация — без прямой
записи в ledger):

```python
create_sale(*, customer_name, customer_phone="", comment="", by) -> Sale     # draft
add_part_item_to_sale(sale, item, *, unit_price, by) -> SaleLine
add_stock_lot_to_sale(sale, lot, quantity, *, unit_price, by) -> SaleLine
remove_sale_line(line, *, by)
create_sale_from_reservation(reservation, *, by) -> Sale                      # draft + строки
complete_sale(sale, *, by) -> Sale                                           # draft → completed
calculate_sale_totals(sale) -> dict          # чистый расчёт из строк (frozen)
rebuild_sale_totals(sale) -> Sale            # пересчёт totals из ЗАМОРОЖЕННЫХ строк
```

**`apps/inventory/services.py`** (новые — расход + ledger, резерв-агностичны):

```python
sell_part_item(item, *, by=None, document_id=None, comment="") -> PartItem
    # lock; status==available обязателен; status→sold; SALE_ITEM (from=loc,to=None,
    #   document_type="sale", document_id); _refresh_balance.
sell_stock_lot(lot, quantity, *, by=None, document_id=None, comment="") -> StockLot
    # lock; status==available; 0<quantity≤lot.quantity; quantity-=; depleted при 0;
    #   SALE_LOT; _refresh_balance.
# + _record_movement расширяется document_type/document_id (поля уже есть).
```

**`complete_sale` (оркестрация, `@transaction.atomic`):**
1. lock `Sale`; должна быть `draft`; строк ≥ 1 (иначе `SaleError`).
2. По каждой строке (под `select_for_update` объекта):
   - проверка доступности с учётом резервов (§11; если `sale.reservation` —
     исключаем свой резерв);
   - **заморозка себестоимости**: `unit_cost_rub` = `landed_cost_rub` /
     `landed_unit_cost_rub` на момент продажи; `total_cost_rub`,
     `total_price = money(unit_price×qty)`, `profit_rub = total_price − total_cost`.
   - физический расход через `inventory.sell_part_item` / `sell_stock_lot`
     (`document_id=sale.pk`).
3. если `sale.reservation` — `reservation.status = converted_to_sale` (+ пересчёт
   reserved через `recompute`/провайдер).
4. `revenue_total/cost_total/profit_total` = сумма строк; `status=completed`,
   `sold_at=now`, `sold_by=by`.

**Обоснование разделения:** цены/документ — это коммерция (`sales`); смена
`status/quantity` и `StockMovement` — это ledger (`inventory`, единая точка истины,
переиспользуемая будущими слоями). Резерв-проверки — в `sales` (только он знает
брони). Все изменения — **только через сервисы**, вьюхи оркеструют.

---

## 13. Транзакции и блокировки

- `complete_sale` — целиком в `transaction.atomic`.
- `select_for_update` на каждом `PartItem`/`StockLot` строки.
- При продаже из резерва — `select_for_update` на `Reservation` (и его строках).
- **Защита от двойной продажи:** `inventory.sell_part_item` требует
  `status==available` под блокировкой → повторная продажа того же экземпляра падает
  (он уже `sold`); `complete_sale` требует `status==draft`.
- **Защита от продажи зарезервированного другим** — проверки §11 под блокировкой.
- Последовательные тесты «нельзя увести `available`/`quantity` в минус».
- Полноценный конкурентный Postgres-тест — на будущий слой (тестовый стек SQLite,
  ограниченная семантика `select_for_update`).

---

## 14. Цены

- `unit_price` **вводит продавец** в строке.
- При добавлении строки/из резерва **подставляем `PartType.recommended_price`**
  как значение по умолчанию (продавец правит).
- `min_price` **показываем как подсказку** в UI.
- **Не блокируем** продажу ниже `min_price` на уровне модели — только
  **предупреждаем** в UI.

**Обоснование:** жёсткий блок ниже `min_price` мешал бы законным скидкам/торгу
(решение Дениса, а не системы). `min_price` — ориентир, не запрет. Блокировку (если
понадобится) проще включить позже как мягкое правило, чем снимать встроенный запрет.

---

## 15. Себестоимость и прибыль

- `unit_cost_rub` **фиксируется в `SaleLine`** на момент продажи (из
  `landed_cost_rub`/`landed_unit_cost_rub`).
- `total_cost_rub` и `profit_rub = total_price − total_cost_rub` фиксируются там же.
- `Sale.revenue_total/cost_total/profit_total` — заморожены при завершении.
- **Будущие изменения себестоимостей старые продажи НЕ пересчитывают** —
  `rebuild_sale_totals` суммирует **уже замороженные** значения строк, а не текущий
  landed cost. Это сохраняет историческую корректность прибыли.

---

## 16. UI (`apps/sales`, шаблоны `templates/sales/`)

| Экран | URL (`name`) | Право |
|---|---|---|
| Список продаж | `/sales/sales/` (`sale_list`) | просмотр — вошедшие |
| Карточка продажи | `…/<pk>/` (`sale_detail`) | просмотр — вошедшие |
| Создать draft | `…/new/` (`sale_create`) | `manage_sales` |
| Добавить `PartItem` | POST `…/<pk>/add-item/` | `manage_sales` |
| Добавить `StockLot` qty | POST `…/<pk>/add-lot/` | `manage_sales` |
| Продажа из резерва | POST `/sales/reservations/<pk>/sell/` (`sale_from_reservation`) | `manage_sales` |
| Завершить продажу | POST `…/<pk>/complete/` | `manage_sales` |
| Снять строку | POST `…/lines/<pk>/remove/` | `manage_sales` |

- Карточка показывает строки (деталь, экземпляр/лот, кол-во, **цена/сумма**) и
  **totals**.
- **Себестоимость и прибыль** (`unit_cost_rub`/`profit_rub`/`cost_total`/
  `profit_total`) — **только при `can_view_purchase_cost`** (контекст `show_costs`).
  Продавец видит **выручку/цену**, но не себестоимость/прибыль.
- Завершённая продажа — без кнопок правки (immutable).
- No-JS: server-rendered формы.

---

## 17. Интеграция с `/search/`

- В результат поиска добавить действие **«Продать»** (и/или «Создать резерв») —
  **только для ролей с правом** (`manage_sales` / `manage_reservations`).
- Достаточно **простой ссылки** на создание продажи/добавление позиции (без
  глубокой интеграции) — не усложняем.
- После продажи `/search/` показывает **обновлённое доступное количество** (кэш уже
  пересчитан сервисом). Цена продажи (`recommended_price`) в поиске уже видна
  (Слой 13); себестоимость по-прежнему скрыта без права.

---

## 18. Права

Новая возможность **`MANAGE_SALES`** (`roles.py`, как `MANAGE_RESERVATIONS`):

| Роль | Продавать | Видеть продажи | Себестоимость/прибыль |
|---|---|---|---|
| Администратор | ✅ | ✅ | ✅ |
| Руководитель | ✅ | ✅ | ✅ |
| **Продавец/Мастер** | ✅ | ✅ | **нет** (по `can_view_purchase_cost`) |
| **Кладовщик** | ❌ | ✅ | нет |
| Наблюдатель | ❌ | ✅ | по `can_view_purchase_cost` |

- **Отдельная `MANAGE_SALES`, а не переиспользование `MANAGE_RESERVATIONS`:**
  продажа меняет физический остаток и фиксирует деньги — это более «тяжёлое»
  право, чем бронь; их полезно разделять (можно дать резерв без права завершать
  продажу). Назначаем `ADMIN`/`MANAGER`/`SELLER`. Без миграции (возможности — код).
- `accounts/models.py`: свойство `can_manage_sales`.
- Просмотр списка/карточки — `login_required`; мутации — под `manage_sales`.
- Себестоимость/прибыль — под `can_view_purchase_cost`.

---

## 19. Инварианты (и кто гарантирует)

| Инвариант | Гарант |
|---|---|
| Нельзя завершить пустую продажу | `complete_sale`: строк ≥ 1 |
| Нельзя продать `receiving` | сервис: `status==available` обязателен |
| Нельзя продать `written_off`/`depleted`/`sold`/`installed`/`quarantine` | `inventory.sell_*`: только `available` |
| Нельзя продать уже проданный `PartItem` | `sell_part_item` под блокировкой (статус уже `sold`) |
| Нельзя продать `PartItem`, зарезервированный **другим** активным резервом | `sales`: `_item_actively_reserved(item, exclude=своя бронь)` |
| Нельзя продать `StockLot` больше доступного | `sell_stock_lot`: `qty ≤ lot.quantity`; `sales`: минус чужой резерв |
| Нельзя продать из резерва больше зарезервированного | `sales`: qty строки ≤ зарезервированного |
| Завершённая продажа **immutable** (кроме будущего сторно) | сервисы: мутации только при `draft`; `StockMovement` append-only |
| Продажа **создаёт** `StockMovement` | `inventory.sell_*` |
| Продажа **меняет** physical (`status`/`quantity`) | `inventory.sell_*` |
| Продажа **фиксирует** себестоимость и прибыль | `complete_sale` (заморозка в `SaleLine`) |
| Продажа **не** оплата/чек | границы слоя (нет платёжных моделей) |

---

## 20. Management-команды

- **Не требуются.** Totals замораживаются при завершении, фоновых пересчётов нет.
- Опционально — `rebuild_sale_totals <id>` для разовой починки агрегатов из
  **замороженных** строк (не из текущего landed cost). Вводим **только если**
  понадобится сопровождение данных; по умолчанию — не делаем (минимализм).

---

## 21. Тесты (`tests/test_sales.py`)

1. Можно создать draft-продажу.
2. Нельзя завершить пустую продажу (`SaleError`).
3. Можно добавить `PartItem` в продажу.
4. `complete_sale` продаёт `PartItem` (статус `sold`).
5. Проданный `PartItem` больше **не доступен** (нет в physical/`available`).
6. Создаётся `StockMovement` типа `SALE_ITEM` (`from`=ячейка, `to`=null,
   `document_type="sale"`, `document_id=Sale.id`, qty=1).
7. `StockBalance` уменьшается (physical/available после продажи).
8. `SaleLine` фиксирует **себестоимость** (`unit_cost_rub` из landed).
9. `SaleLine` фиксирует **прибыль** (`profit_rub = total_price − total_cost`).
10. Можно продать `StockLot` quantity (частично).
11. `StockLot.quantity` уменьшается на проданное.
12. `StockLot` → `depleted` при нуле; движение `SALE_LOT`.
13. Нельзя продать `StockLot` больше доступного.
14. Нельзя продать `receiving` (и терминальные статусы).
15. Нельзя продать зарезервированное **другим** активным резервом.
16. Можно создать продажу из активного `Reservation` (строки скопированы).
17. `Reservation` → `converted_to_sale` после завершения.
18. `quantity_reserved` освобождается (кэш) после конверсии.
19. Завершённая продажа **immutable** (повторное завершение/правка строк → ошибка).
20. Продавец может создать/завершить продажу; кладовщик — **403**, но видит список.
21. Себестоимость/прибыль скрыты без `can_view_purchase_cost` (продавец не видит).
22. Продажа **не** реализует оплату/чек (нет платёжных полей/экранов).
23. **Архитектурный мок:** `complete_sale` вызывает `inventory.sell_*`, а вьюха
    сама `StockMovement`/`StockBalance` не пишет (`patch` сервиса → ledger неизменен).
24. Hidden/query-параметры перепроверяются сервером (подмена `sale`/`item`/`lot`/
    `qty`/`unit_price` → ошибка/404, без эффекта).

---

## 22. Ручная проверка

1. Продавцом → `/search/` найти деталь → «Продать» → создать draft.
2. Добавить `available`-экземпляр и количество из лота; цены подставились из
   `recommended_price`, видна подсказка `min_price`; продавец правит цену.
3. Завершить → в карточке totals (выручка; себестоимость/прибыль — **не видны**
   продавцу); в «Движениях» — `SALE_ITEM`/`SALE_LOT` с `document=sale`,
   `from`→`—`; в `/search/` «доступно» уменьшилось, «физически» уменьшилось.
4. Войти админом → в карточке видны себестоимость и прибыль.
5. Создать бронь (Слой 15) → «Продать из резерва» → завершить → резерв стал
   «Продан», `quantity_reserved` освободился, остаток списан, движение создано.
6. Попробовать продать тот же экземпляр снова → отказ (уже продан).
7. Попробовать продать зарезервированное другим резервом напрямую → отказ.
8. Кладовщиком → действия продажи недоступны (403), список виден.

---

## 23. Критерии готовности

1. Продажа `PartItem`/`StockLot` и из резерва идёт **только через сервисы**:
   документ — `sales`, физика/ledger — `inventory.sell_*`; вьюха
   `StockMovement`/`StockBalance` не пишет (мок-тест §21.23).
2. `PartItem` → `sold` и недоступен; `StockLot.quantity` падает, `depleted` при 0;
   создаётся `SALE_ITEM`/`SALE_LOT` с `document=sale`; баланс пересчитан.
3. Себестоимость/прибыль заморожены в `SaleLine`; totals на `Sale`; ретро-пересчёта
   нет (§15).
4. Резерв: продажа из активного резерва конвертирует его в `converted_to_sale` и
   освобождает `reserved`; нельзя продать больше зарезервированного/доступного;
   нельзя продать чужое зарезервированное.
5. Права: `MANAGE_SALES` (Админ/Руководитель/Продавец); кладовщик/наблюдатель не
   продают; себестоимость/прибыль — под `can_view_purchase_cost`.
6. Завершённая продажа immutable; инварианты §19 соблюдены.
7. Границы: нет оплаты/кассы/чеков/возвратов/установок/гарантии/списаний вне
   продажи/инвентаризации/аналитики/PDF/CRM/FIFO; `StockBalance` не источник истины.
8. Тесты зелёные; `ruff`/`djlint` чисты; `manage.py check` ок; `makemigrations
   --check` — миграции **только** `apps/sales` (Sale/SaleLine + seed) и **одна**
   `apps/inventory` (типы движения `SALE_*`).

---

## 24. Файлы (создаются/изменяются)

**Изменяются — `apps/inventory`:**
- `models.py` — `MovementType += SALE_ITEM, SALE_LOT`.
- `migrations/0005_alter_stockmovement_movement_type.py` — `AlterField` choices.
- `services.py` — `sell_part_item`, `sell_stock_lot`; `_record_movement`
  расширить `document_type`/`document_id`.

**Изменяются — `apps/sales`:**
- `models.py` — `Sale`, `SaleLine`.
- `services.py` — `create_sale`, `add_part_item_to_sale`, `add_stock_lot_to_sale`,
  `remove_sale_line`, `create_sale_from_reservation`, `complete_sale`,
  `calculate_sale_totals`, `rebuild_sale_totals`; `_active_reserved_for_lot`
  расширить `exclude=`.
- `forms.py`, `views.py`, `urls.py`, `admin.py`.
- `migrations/0003_sale_saleline.py`, `migrations/0004_seed_sale_sequence.py`.

**Изменяются — `apps/accounts`:**
- `roles.py` — `MANAGE_SALES` + привязка `ADMIN`/`MANAGER`/`SELLER`.
- `models.py` — `can_manage_sales`.
- `context_processors.py` — пункт «Продажи» (заменяет заглушку «Продажа»).

**Создаются — шаблоны `templates/sales/`:**
- `sale_list.html`, `sale_detail.html`, `sale_form.html`.
- `templates/core/search.html` — действие «Продать» (под правом).

**Тесты:** `tests/test_sales.py`.

**Без изменений:** `StockMovement` append-only/ограничения; `PartItem.Status.SOLD`
и `StockLot.Status.DEPLETED` (уже есть → без модельных правок этих моделей);
провайдер reserved Слоя 15 (переиспользуем).

---

## 25. Что будет закоммичено

Два коммита (как в Слоях 5–15):
1. `План Слоя 16: продажи` — этот файл (push в `origin/main` до реализации).
2. `Слой 16: продажи` — реализация (после `pytest`, `ruff`, `djlint`,
   `makemigrations --check`, `manage.py check`), затем **push в `origin/main`**.

Останавливаемся перед **Слоем 17**.

---

## Границы Слоя 16 (чего НЕ делаем)

- Не реализуем оплату, кассу, чеки, возвраты, установки, гарантию, списания вне
  продажи, инвентаризацию, аналитику продаж, PDF-документы, CRM.
- Не делаем FIFO-автоподбор `PartType` (продаём конкретный item/lot).
- Не реализуем отмену/сторно проведённой продажи (слой возвратов).
- **Не пишем `StockMovement`/`StockBalance`/`PartItem.status`/`StockLot.quantity`
  напрямую из `apps/sales`** — только через `inventory.sell_*`.
- Не превращаем `StockBalance` в источник истины.

---

## Открытые вопросы — ЗАФИКСИРОВАНЫ заказчиком (2026-06-25)

Все согласованы перед реализацией:

1. **Тип движения:** ✅ `SALE_ITEM` / `SALE_LOT` (парность с `RECEIVE_*`/`MOVE_*`).
2. **Где физика:** ✅ новые `inventory.sell_*` (ledger в одном месте, переиспользуемо).
3. **Номер:** ✅ **`S-000001`** (ASCII — удобно для ссылок/поиска/логов; UI «Продажа S-…»).
4. **Отмена продажи:** ✅ только `draft → completed`; сторно/возврат — будущий слой.
5. **`min_price`:** ✅ предупреждать в UI, **не блокировать**.
6. **`current_location` после продажи:** ✅ оставить как последнюю ячейку (аудит).
7. **Право:** ✅ отдельная `MANAGE_SALES` (Админ/Руководитель/Продавец).
8. **Конверсия резерва:** ✅ только целиком (частичная — будущее).
9. **`/search/` «Продать»:** ✅ простая ссылка/действие под правом (без быстрого checkout).
