# План реализации — Слой 17. Выдача деталей в ремонт / установка на технику

**Статус:** УТВЕРЖДЁН (2026-06-26) · все рекомендации приняты · реализация в границах §19.

---

## 1. Цель слоя

Сделать сценарий, когда деталь **не продаётся как товар**, а **выдаётся со склада
и устанавливается** в рамках ремонта техники клиента. Это **физическое выбытие**
из доступного остатка по **иной бизнес-причине**, чем продажа.

Пример: клиент привёз квадроцикл/снегоход/лодку. Денис берёт деталь со склада и
ставит её на эту технику. Склад должен понимать, что деталь **больше не доступна**,
а система — хранить **куда** (на какую технику) и **зачем** (какой ремонт) ушла.

**Главная мысль:** Слой 17 — это **складской расход в ремонт/установку**, а **не
продажа, не оплата и не чек**. Денег (цены/выручки/прибыли) здесь нет — только
**фиксация себестоимости** выданного.

**Философия «временно или окончательно» (решено заранее):** выдача трактуется как
**окончательная установка/расход** — `physical` уменьшается, экземпляр переходит в
`installed`. Возврат неиспользованной детали со «временной выдачи» — **отдельный
будущий слой** (как и возвраты продаж). `StockBalance.quantity_in_repair` в этом
слое **не используем** (остаётся 0; он зарезервирован под будущую «временную
выдачу мастеру»). Обоснование §11.

**Главный архитектурный контроль (как в Слоях 10/12/14/15/16):** физическое
списание и запись ledger идут **только через сервисы `apps/inventory`** (новые
`issue_part_item`/`issue_stock_lot`). **Приложение ремонта документ ведёт само, но
ledger напрямую НЕ трогает.** View — оркестратор; контроль — тест-мок (§23).

### Что уже есть (переиспользуем)

| Уже реализовано | Где | Роль в Слое 17 |
|---|---|---|
| `PartItem.Status.INSTALLED` / `REPAIR` (в enum) | `inventory/models.py` | целевой статус выданного экземпляра (модель **не меняем**) |
| `StockLot.Status.DEPLETED` + обнуление при 0 | `inventory` | статус лота при полном расходе |
| `StockMovement.document_type/document_id` + `_record_movement(..., document_type, document_id)` | `inventory` (Слой 16) | связь движения с `RepairOrder` |
| `sell_part_item` / `sell_stock_lot` (расход + ledger) | `inventory/services.py` (Слой 16) | паттерн повторяем; общий приватный helper (§12) |
| Резерв-хелперы `_item_actively_reserved`, `_active_reserved_for_lot` | `apps/sales` (Слой 15) | проверка «не зарезервировано» — выносим в **public** |
| `StockBalance.quantity_in_repair` (поле есть, всегда 0) | `inventory` | **не используем** в Слое 17 (final-расход) |
| `NumberSequence`, `money()`, `can_view_purchase_cost` | `inventory`/`procurement`/`accounts` | номер заказа, округление, скрытие себестоимости |
| `catalog.VehicleType/VehicleMake/VehicleModel` | `catalog` | тип техники (controlled), см. §3/§4 |

### Что нового в Слое 17

- Новое приложение **`apps/repairs`**: модели `RepairOrder`, `RepairIssueLine`,
  сервисы, вьюхи, шаблоны.
- Новые типы движения **`ISSUE_ITEM` / `ISSUE_LOT`** (`inventory.MovementType`) →
  **миграция инвентаря** (`AlterField` choices).
- Сервисы `issue_part_item` / `issue_stock_lot` в `apps/inventory` (общий с `sell_*`
  приватный helper `_consume_*`).
- **Public-обёртки** резерв-проверок в `apps/sales` (`is_part_item_reserved`,
  `active_reserved_for_lot`) для переиспользования из `apps/repairs`.
- Возможность **`MANAGE_REPAIRS`**, экраны ремонта, действие «Выдать в ремонт» в
  `/search/`.

**Чего слой НЕ делает:** оплата, касса, чеки, продажа, возвраты, сторно, гарантия,
списания вне ремонтной выдачи, инвентаризация, аналитика, PDF, CRM, сложный
заказ-наряд (нормы времени/услуги/работы), FIFO-автоподбор `PartType`; не
превращает `StockBalance` в источник истины.

---

## 2. Имя доменной области — **рекомендация: `apps/repairs`**

| Вариант | За | Против | Вывод |
|---|---|---|---|
| **`apps/repairs` (рекомендация)** | имена `RepairOrder`/`RepairIssueLine` согласованы; «ремонт» точно описывает сценарий; рус. «Ремонт» в UI | чуть уже, чем «мастерская» | **выбираем** |
| `apps/workshop` | шире (мастерская/любые работы), задел под будущие заказ-наряды | расходится с именем `RepairOrder`; для Слоя 17 шире нужного | альтернатива (откр.) |
| `apps/service` | общо | перегружено (`services.py`, «сервис-слой»), двусмысленно | нет |
| `apps/sales` | уже есть | это **не продажа**; смешало бы два бизнес-потока | нет |

**Обоснование.** Выдача в ремонт — отдельная бизнес-причина, не продажа, поэтому
**не** в `apps/sales`. `apps/repairs` именует домен прямо и согласуется с моделями
`RepairOrder`/`RepairIssueLine`. Зависимости: `repairs → inventory` (FK + сервисы)
и `repairs → sales` (только public-проверка резервов, §16) — оба **ацикличны**
(`sales`/`inventory` не импортируют `repairs`).

---

## 3. Сущности

Две модели в `apps/repairs/models.py`. **Клиент и техника — без CRM/автопарка.**

### 3.1 `RepairOrder` (шапка ремонтного заказа)

- Клиент — **текстовые поля** (как в резервах/продажах): `customer_name`,
  `customer_phone`.
- Техника клиента: **`vehicle_type` — опциональный FK на `catalog.VehicleType`**
  (это маленький controlled-список: снегоход/лодка/квадроцикл — уместно
  переиспользовать), а **марка/модель/идентификатор — свободный текст**
  (`vehicle_make`, `vehicle_model`, `vehicle_identifier`/VIN).

  **Обоснование:** `VehicleMake`/`VehicleModel` в каталоге — это словарь
  **совместимости деталей** (controlled), а конкретная машина клиента произвольна;
  тащить её в каталог совместимости = засорение каталога и шаг к CRM/автопарку.
  Поэтому тип — FK (стабильный список), а make/model/VIN — текст (произвольная
  машина клиента). Альтернатива «всё текстом» — проще, но теряет фильтр по типу.

### 3.2 `RepairIssueLine` (строка выдачи/установки)

XOR `part_item`/`stock_lot` (как `SaleLine`/`StockMovement`). **Без цены/прибыли** —
только себестоимость.

---

## 4. Поля `RepairOrder`

| Поле | Тип | Назначение |
|---|---|---|
| `number` | `CharField` unique, editable=False (`R-000001`, ASCII — как `S-`) | номер заказа |
| `status` | `CharField(choices=Status)` | `draft`/`completed`/`canceled` (§7) |
| `customer_name` | `CharField` | клиент (обязателен) |
| `customer_phone` | `CharField` blank | контакт |
| `vehicle_type` | FK `catalog.VehicleType`, `PROTECT`, null/blank | тип техники (опц.) |
| `vehicle_make` | `CharField` blank | марка (текст) |
| `vehicle_model` | `CharField` blank | модель (текст) |
| `vehicle_identifier` | `CharField` blank | VIN/серийный номер техники (опц.) |
| `problem_description` | `CharField`/`TextField` blank | что чинят |
| `comment` | `CharField` blank | примечание |
| `cost_total` | `Decimal(14,2)` default 0 | сумма себестоимости выданного (заморожена) |
| `created_by` | FK user, `SET_NULL` | кто создал |
| `created_at` / `updated_at` | auto | аудит |
| `completed_at` | `DateTimeField` null/blank | момент проведения |
| `canceled_at` | `DateTimeField` null/blank | момент отмены (черновика) |

Номер `R-000001` — ASCII, единообразно с `S-` (удобно для ссылок/поиска/логов); в
UI «Заказ R-000001». Отдельный ключ `NumberSequence "repair_order"` (seed-миграция).

---

## 5. Поля `RepairIssueLine`

| Поле | Тип | Назначение |
|---|---|---|
| `repair_order` | FK `RepairOrder`, `CASCADE`, related_name="lines" | шапка |
| `part_type` | FK `catalog.PartType`, `PROTECT` | денормализация |
| `part_item` | FK `inventory.PartItem`, `PROTECT`, null | поштучно (XOR) |
| `stock_lot` | FK `inventory.StockLot`, `PROTECT`, null | количественно (XOR) |
| `batch` / `batch_line` | FK `procurement.*`, `PROTECT` | денормализация для себестоимости |
| `quantity` | `Decimal(12,3)` | 1 для экземпляра; дробное для лота |
| `unit_cost_rub` | `Decimal(12,2)` editable=False | себестоимость за ед., **заморожена при выдаче** |
| `total_cost_rub` | `Decimal(14,2)` editable=False | = `money(unit_cost × quantity)` |
| `note` | `CharField` blank | примечание к позиции |
| `issued_at` | `DateTimeField` null/blank | момент выдачи (= проведение заказа) |
| `created_at` | auto | аудит |

**Без `unit_price`/`total_price`/`profit_rub`** — это не продажа (§15).
`issued_by` отдельно не вводим — достаточно `RepairOrder.created_by` + движение
`created_by` (кто провёл); открытый вопрос, если нужен отдельный установщик.

**Ограничения БД:** `CheckConstraint` XOR(`part_item`,`stock_lot`) + `quantity > 0`.

---

## 6. Что можно выдавать в ремонт

| Объект | Как | Доступность |
|---|---|---|
| **`PartItem`** целиком (`quantity=1`) | скан/выбор экземпляра | `available` и не зарезервирован (§16/§20) |
| **`StockLot`** quantity (Decimal 12,3), частично/целиком | выбор лота + кол-во | `qty ≤ lot.quantity − активный_резерв` |

**`PartType` без выбора item/lot не выдаём** — это потребовало бы FIFO-автоподбора
(будущий слой). Мастер выбирает **конкретный** экземпляр/лот.

---

## 7. Статусы `RepairOrder` — минимальный набор

```python
class Status(models.TextChoices):
    DRAFT     = "draft",     "Черновик"     # собираем позиции; склад НЕ трогаем
    COMPLETED = "completed", "Проведён"      # выдача проведена (расход списан)
    CANCELED  = "canceled",  "Отменён"       # черновик отменён (склад не трогали)
```

**Обоснование (зеркально продажам, без автосервис-workflow):**

- **`draft`** — собираем строки; **физического расхода ещё нет** (как в Слое 16:
  выдача происходит при проведении). Это позволяет собрать заказ и затем
  единым атомарным действием списать.
- **`completed`** — единственное состояние, которое **списывает остаток**;
  проведённый заказ **immutable** (отмена/возврат установленного — будущий слой).
- **`canceled`** — отмена **черновика** (склад не затронут).
- **`in_progress` НЕ вводим** — при «выдаче-при-проведении» отдельное «в работе»
  не несёт складского смысла; настоящий статусный workflow автосервиса — будущее.

> Альтернатива «выдача сразу при добавлении позиции» (расход на add, заказ —
> контейнер) рассмотрена и **отклонена**: ломает единообразие с продажами и не даёт
> собрать/проверить заказ до списания. Выдаём **при проведении** (открытый вопрос).

---

## 8. Влияние на `PartItem`

- При проведении заказа экземпляр переходит в **`installed`** (статус уже в enum,
  модель **не меняем**) — «установлен на технику клиента».
- Статус ставится **сервисом `inventory.issue_part_item`** (производитель
  `installed`); ручная `ALLOWED_TRANSITIONS` не используется.
- `installed` **не входит** в `ITEM_PHYSICAL_STATUSES` → экземпляр сразу выбывает
  из physical/available (главное: после выдачи **не доступен** для продажи).
- **`current_location` оставляем** как последнюю складскую ячейку (аудит), не
  очищаем — доступность определяется статусом, а ячейка хранит историю «откуда
  выдан» (плюс `from_location` в движении). То же решение, что для продажи.

**Почему `installed`, а не `repair`/`in_repair`:** выбрана философия
**окончательной установки** (§11). `repair`/`quantity_in_repair` означали бы
«временно у мастера, может вернуться» — это другой (будущий) сценарий.

---

## 9. Влияние на `StockLot`

- `quantity` уменьшается на выданное количество **через сервис**
  `inventory.issue_stock_lot`.
- При `quantity == 0` → статус **`depleted`**.
- **Частичная выдача разрешена**; лот **не дробим** на новый лот.
- Создаётся `StockMovement` типа `ISSUE_LOT` (§10).

---

## 10. `StockMovement`

| Поле движения | Значение |
|---|---|
| `movement_type` | **`ISSUE_ITEM`** / **`ISSUE_LOT`** |
| `from_location` | ячейка детали (`item.current_location` / `lot.location`) |
| `to_location` | **`null`** (выбытие со склада) |
| `quantity` | выданное количество (1 / qty) |
| `unit_cost_rub` | `landed_cost_rub` / `landed_unit_cost_rub` |
| `total_cost_rub` | `unit_cost × quantity` (в `StockMovement.save()`) |
| `document_type` | **`"repair_order"`** |
| `document_id` | `RepairOrder.id` |

**Название `ISSUE_ITEM`/`ISSUE_LOT` (рекомендация, как и просил заказчик).**
Обоснование: это общее «**выдано со склада в работу**», а *установка* — бизнес-
контекст **документа** (`document_type="repair_order"`), а не самого движения. Так
будущие выдачи (например, во внутренние нужды) смогут переиспользовать `ISSUE_*`
или добавить свой `document_type`, не плодя типы движения. Альтернатива
`INSTALL_*` жёстко привязала бы движение к установке (в открытых вопросах).

**Минимальная правка инвентаря:** `MovementType += ISSUE_ITEM, ISSUE_LOT` →
миграция `inventory/0006_alter_stockmovement_movement_type.py` (только `AlterField`).

---

## 11. `StockBalance` и философия «временно/окончательно»

**Решение — выдача = окончательный расход** (рекомендация заказчика):

- `StockBalance` остаётся **кэшем** (не источник истины).
- `quantity_physical` уменьшается (экземпляр `installed` исключён / `lot.quantity`
  упало) через `inventory.issue_*` → `_refresh_balance`.
- `quantity_available` пересчитывается (`physical − quarantine − reserved`).
- **`quantity_in_repair` НЕ используем** (остаётся 0).

**Честный разбор альтернативы.** Если бы «выдача в ремонт» означала **временно**
(мастер может вернуть деталь), тогда нужно было бы держать
`quantity_in_repair` отдельно: `physical` не падает, но `available` уменьшается, а
деталь «висит» у мастера до возврата/окончательной установки. Это сложнее (нужен
возврат, два состояния, сверка) и тянет за собой целый слой возвратов.

**Для Слоя 17 берём окончательную установку/расход** — просто, согласуется с
продажей (тот же `_consume_*`), без второго состояния. **Возврат неиспользованной
детали** (и тогда — использование `quantity_in_repair`) — **отдельный будущий
слой**. Поле `quantity_in_repair` остаётся заделом под него.

---

## 12. Сервисы `apps/inventory` — общий helper с продажей

Логика расхода для продажи и для выдачи почти одинакова (различаются **целевой
статус**, **тип движения**, **document_type**). Чтобы не дублировать и **не сломать
продажи**, выделяем приватные helpers, а `sell_*`/`issue_*` — тонкие обёртки:

```python
def _consume_part_item(item, *, new_status, movement_type, document_type,
                       by=None, document_id=None, comment="") -> PartItem:
    # lock; status==available; from_location=item.current_location;
    # item.status=new_status; _record_movement(... to=None, document_type, document_id);
    # _refresh_balance.

def _consume_stock_lot(lot, quantity, *, movement_type, document_type,
                       by=None, document_id=None, comment="") -> StockLot:
    # lock; status==available; 0<quantity≤lot.quantity; quantity-=; depleted при 0;
    # _record_movement; _refresh_balance.

# Продажа (Слой 16) — обёртки над helper:
def sell_part_item(item, *, by=None, document_id=None, comment=""):
    return _consume_part_item(item, new_status=SOLD, movement_type=SALE_ITEM,
                              document_type="sale", by=by, document_id=document_id, comment=comment)
def sell_stock_lot(lot, quantity, *, by=None, document_id=None, comment=""):
    return _consume_stock_lot(lot, quantity, movement_type=SALE_LOT,
                              document_type="sale", by=by, document_id=document_id, comment=comment)

# Выдача в ремонт (Слой 17):
def issue_part_item(item, *, by=None, document_id=None, comment=""):
    return _consume_part_item(item, new_status=INSTALLED, movement_type=ISSUE_ITEM,
                              document_type="repair_order", by=by, document_id=document_id, comment=comment)
def issue_stock_lot(lot, quantity, *, by=None, document_id=None, comment=""):
    return _consume_stock_lot(lot, quantity, movement_type=ISSUE_LOT,
                              document_type="repair_order", by=by, document_id=document_id, comment=comment)
```

- **Безопасность рефактора:** поведение `sell_*` не меняется (обёртки передают
  `SOLD`/`SALE_*`/`"sale"`), его гарантируют **265 существующих тестов** (включая
  `tests/test_sales.py`). Если предпочесть нулевой риск — можно сделать `issue_*`
  отдельными функциями без рефактора `sell_*` (дублирование) — открытый вопрос;
  рекомендация — общий helper.
- `issue_*` работают в `transaction.atomic`, под `select_for_update`, меняют
  physical, создают `ISSUE_*`, обновляют `StockBalance`, **не знают о резервах**
  (резерв-проверки делает `apps/repairs` до вызова).

---

## 13. Сервисы `apps/repairs/services.py`

```python
class RepairError(Exception): ...

create_repair_order(*, customer_name, customer_phone="", vehicle_type=None,
                    vehicle_make="", vehicle_model="", vehicle_identifier="",
                    problem_description="", comment="", by) -> RepairOrder   # draft
add_part_item_to_repair_order(order, item, *, note="", by) -> RepairIssueLine
add_stock_lot_to_repair_order(order, lot, quantity, *, note="", by) -> RepairIssueLine
remove_repair_line(line, *, by)
complete_repair_order(order, *, by) -> RepairOrder        # draft → completed (выдача)
cancel_repair_order(order, *, by)                         # draft → canceled
calculate_repair_costs(order) -> Decimal                  # сумма из замороженных строк
```

**`complete_repair_order` (оркестрация, `@transaction.atomic`):**
1. lock заказ; должен быть `draft`; строк ≥ 1 (иначе `RepairError`).
2. по каждой строке (под `select_for_update` объекта):
   - доступность с учётом резервов (§16): экземпляр `available` **и**
     `not sales.is_part_item_reserved(item)`; лот `qty ≤ lot.quantity −
     sales.active_reserved_for_lot(lot)`;
   - **заморозка себестоимости**: `unit_cost_rub`/`total_cost_rub` из landed;
   - физический расход через `inventory.issue_part_item`/`issue_stock_lot`
     (`document_id=order.pk`); `issued_at=now`.
3. `cost_total` = сумма строк; `status=completed`, `completed_at=now`.

- Все действия — **только через сервисы**; вьюхи ledger не пишут (тест §23).
- `apps/repairs` импортирует **public** `is_part_item_reserved`/
  `active_reserved_for_lot` из `apps/sales` (см. §16).

---

## 14. Себестоимость

- `unit_cost_rub` **фиксируется** в `RepairIssueLine` при выдаче (из
  `landed_cost_rub`/`landed_unit_cost_rub`); `total_cost_rub` тоже.
- `RepairOrder.cost_total` — заморожен при проведении.
- **Будущие изменения landed cost историю ремонта не меняют** (как в продажах).
- Себестоимость видна **только при `can_view_purchase_cost`** (контекст
  `show_costs`).

---

## 15. Цены/выручка — НЕТ

На этом слое **нет** продажной цены работы, **нет** оплаты, **нет** чека, **нет**
прибыли по ремонту.

**Обоснование:** Слой 17 — **складская выдача в ремонт**, а не полноценный
заказ-наряд автосервиса с оплатой/услугами/нормо-часами. Мы фиксируем только
**факт расхода и его себестоимость** (куда ушла деталь). Денежная сторона ремонта
(стоимость работ, оплата, документ для клиента) — отдельные будущие слои, чтобы не
смешать складской учёт с кассой/услугами.

---

## 16. Резервы

- **Нельзя выдать в ремонт деталь, зарезервированную активным `Reservation`**
  (экземпляр) или количество, которое увело бы `available` лота ниже 0.
- Проверки — переиспользуем резерв-логику Слоя 15 через **public-обёртки** в
  `apps/sales`:
  - `is_part_item_reserved(item) -> bool` (обёртка над `_item_actively_reserved`);
  - `active_reserved_for_lot(lot) -> Decimal` (обёртка над `_active_reserved_for_lot`).
- **Ремонт НЕ связываем с `Reservation`** (не «выдать из резерва»): резерв — это
  бронь под продажу клиенту, выдача в ремонт — другой поток. Смешивать их в Слое 17
  не будем (выдача из резерва, если понадобится, — будущее). Так два бизнес-потока
  остаются раздельными.

> Зависимость `repairs → sales` (только эти две public-функции) ацикл��чна; `sales`
> про `repairs` ничего не знает.

---

## 17. UI (`apps/repairs`, шаблоны `templates/repairs/`)

| Экран | URL (`name`) | Право |
|---|---|---|
| Список заказов | `/repairs/orders/` (`repair_order_list`) | просмотр — вошедшие |
| Карточка заказа | `…/<pk>/` (`repair_order_detail`) | просмотр — вошедшие |
| Создать заказ | `…/new/` (`repair_order_create`) | `manage_repairs` |
| Добавить `PartItem` | POST `…/<pk>/add-item/` | `manage_repairs` |
| Добавить `StockLot` qty | POST `…/<pk>/add-lot/` | `manage_repairs` |
| Снять позицию | POST `…/lines/<pk>/remove/` | `manage_repairs` |
| Провести (выдать) | POST `…/<pk>/complete/` | `manage_repairs` |
| Отменить (черновик) | POST `…/<pk>/cancel/` | `manage_repairs` |

- Карточка: клиент, техника (тип/марка/модель/VIN), проблема, **выданные позиции**
  (деталь, экземпляр/лот, кол-во); **себестоимость строк и `cost_total` — только
  `can_view_purchase_cost`**.
- Проведённый заказ — без кнопок правки (immutable).
- **Не делаем** сложный заказ-наряд, оплату, чек, услуги/работы/нормо-часы.
- No-JS: server-rendered формы.

---

## 18. Интеграция с `/search/`

- Действие **«Выдать в ремонт»** — только для ролей с `manage_repairs`.
- Простая ссылка на создание заказа (без быстрого checkout).
- После выдачи `/search/` показывает **уменьшенный доступный остаток** (кэш уже
  пересчитан сервисом). Рядом с «Продать» (Слой 16) появляется «Выдать в ремонт».

---

## 19. Права

Новая возможность **`MANAGE_REPAIRS`** (`roles.py`):

| Роль | Выдавать в ремонт | Видеть заказы | Себестоимость |
|---|---|---|---|
| Администратор | ✅ | ✅ | ✅ |
| Руководитель | ✅ | ✅ | ✅ |
| **Кладовщик** | ✅ | ✅ | нет |
| **Продавец/Мастер** | ✅ | ✅ | нет |
| Наблюдатель | ❌ | ✅ | по `can_view_purchase_cost` |

**Обоснование широкого набора (Админ/Руководитель/Кладовщик/Продавец-Мастер):**
выдача в ремонт — это и **складское** действие (кладовщик снимает деталь с полки),
и **мастерское** (Продавец/Мастер ставит деталь на технику). Поэтому, в отличие от
продажи, право даём и кладовщику, и мастеру. Наблюдатель — только просмотр.
Себестоимость — под `can_view_purchase_cost` (кладовщик/мастер её не видят).

- `roles.py`: `MANAGE_REPAIRS` для `ADMIN`/`MANAGER`/`STOREKEEPER`/`SELLER`.
- `accounts/models.py`: `can_manage_repairs`. Без миграции (возможности — код).
- Просмотр — `login_required`; мутации — под `manage_repairs`.

---

## 20. Инварианты (и кто гарантирует)

| Инвариант | Гарант |
|---|---|
| Нельзя завершить пустой заказ | `complete_repair_order`: строк ≥ 1 |
| Нельзя выдать `receiving` | `inventory.issue_*`: только `available` |
| Нельзя выдать `sold`/`installed`/`written_off`/`depleted`/`quarantine` | `issue_*`: только `available` |
| Нельзя выдать уже проданный/установленный `PartItem` | `issue_part_item` под блокировкой (статус не `available`) |
| Нельзя выдать `PartItem`, зарезервированный активным `Reservation` | `repairs`: `sales.is_part_item_reserved(item)` |
| Нельзя выдать `StockLot` больше доступного (с учётом резерва) | `issue_*`: `qty ≤ lot.quantity`; `repairs`: минус `active_reserved_for_lot` |
| Выдача **создаёт** `StockMovement` (`ISSUE_*`) | `inventory.issue_*` |
| Выдача **меняет** physical (`status`/`quantity`) | `inventory.issue_*` |
| Выдача **фиксирует** себестоимость | `complete_repair_order` (заморозка в строке) |
| Проведённый заказ **immutable** | сервисы: мутации только при `draft` |
| Выдача **не** продажа / **не** оплата/чек | границы (нет `Sale`/цены/платёжных полей) |

---

## 21. Транзакции и блокировки

- `complete_repair_order` — целиком в `transaction.atomic`.
- `select_for_update` на каждом `PartItem`/`StockLot`.
- **Защита от двойной выдачи:** `issue_part_item` требует `available` под
  блокировкой (после выдачи статус `installed`); `complete_repair_order` требует
  `draft`.
- **Защита от ухода `available`/`quantity` в минус** — проверки §20 под блокировкой.
- Последовательные тесты на отсутствие минуса; конкурентный Postgres-тест —
  будущий слой (тестовый стек SQLite).

---

## 22. Management-команды

- **Не требуются.** Себестоимость замораживается при проведении, фоновых пересчётов
  нет. Опционально — `rebuild_repair_costs` для разовой починки агрегата из
  **замороженных** строк (вводим только при необходимости; по умолчанию — нет).

---

## 23. Тесты (`tests/test_repairs.py`)

1. Можно создать ремонтный заказ (draft).
2. Нельзя завершить пустой заказ (`RepairError`).
3. Можно добавить `PartItem` в заказ.
4. `complete_repair_order` выдаёт `PartItem`.
5. `PartItem` становится `installed`.
6. `PartItem` больше **не доступен** (нет в physical/`available`).
7. Создаётся `StockMovement` типа `ISSUE_ITEM` (`from`=ячейка, `to`=null,
   `document_type="repair_order"`, `document_id=order.id`, qty=1).
8. `StockBalance` уменьшается.
9. `RepairIssueLine` фиксирует себестоимость (`unit_cost_rub`/`total_cost_rub`).
10. Можно выдать `StockLot` quantity (частично).
11. `StockLot.quantity` уменьшается.
12. `StockLot` → `depleted` при нуле; движение `ISSUE_LOT`.
13. Нельзя выдать `StockLot` больше доступного.
14. Нельзя выдать `receiving`.
15. Нельзя выдать зарезервированное (active `Reservation`).
16. Кладовщик/мастер/админ с правом могут выдать.
17. Пользователь без `manage_repairs` — **403** (но видит список).
18. Себестоимость скрыта без `can_view_purchase_cost`.
19. **Архитектурный мок:** при проведении вьюха вызывает сервис и сама
    `StockMovement`/`StockBalance` не пишет (`patch` сервиса → ledger неизменен).
20. Hidden/query-параметры перепроверяются сервером (подмена заказа/item/lot/qty →
    ошибка/404, без эффекта).
21. **Выдача не создаёт `Sale`** (нет продаж/цены/прибыли).
22. **Выдача не создаёт оплату/чек** (в модели нет платёжных полей).
23. Регресс: продажи (`tests/test_sales.py`) остаются зелёными после рефактора
    `sell_*`/`_consume_*`.

---

## 24. Ручная проверка

1. Кладовщиком/мастером → `/search/` найти деталь → «Выдать в ремонт» → создать
   заказ (клиент, тип техники, марка/модель/VIN, проблема).
2. Добавить `available`-экземпляр и количество из лота → провести.
3. В «Движениях» — `ISSUE_ITEM`/`ISSUE_LOT` с `document=repair_order`, `from`→`—`;
   в `/search/` «доступно» уменьшилось, «физически» уменьшилось; экземпляр —
   `installed`, лот — меньше/`depleted`.
4. Себестоимость строк/`cost_total`: кладовщику/мастеру не видна; админу — видна.
5. Попробовать выдать тот же экземпляр снова → отказ (уже `installed`).
6. Попробовать выдать зарезервированную деталь → отказ.
7. Наблюдателем → действия выдачи недоступны (403), список виден.

---

## 25. Критерии готовности

1. Выдача `PartItem`/`StockLot` идёт **только через сервисы**: документ — `repairs`,
   физика/ledger — `inventory.issue_*`; вьюха ledger не пишет (мок-тест §23.19).
2. `PartItem` → `installed` и недоступен; `StockLot.quantity` падает, `depleted`
   при 0; создаётся `ISSUE_ITEM`/`ISSUE_LOT` с `document=repair_order`; баланс
   пересчитан.
3. Себестоимость заморожена в строке; `cost_total` на заказе; ретро-пересчёта нет.
4. Резерв: нельзя выдать зарезервированное; ремонт **не** связан с `Reservation`.
5. Права: `MANAGE_REPAIRS` (Админ/Руководитель/Кладовщик/Продавец-Мастер);
   наблюдатель не выдаёт; себестоимость — под `can_view_purchase_cost`.
6. Проведённый заказ immutable; инварианты §20 соблюдены; **нет** цены/выручки/
   прибыли/оплаты/чека.
7. Границы: нет продажи/оплаты/кассы/чеков/возвратов/сторно/гарантии/услуг/
   нормо-часов/инвентаризации/аналитики/PDF/CRM/FIFO; `StockBalance` не источник
   истины.
8. Тесты зелёные (вкл. регресс продаж); `ruff`/`djlint` чисты; `manage.py check`
   ок; `makemigrations --check` — миграции **только** `apps/repairs` (+ seed) и
   **одна** `apps/inventory` (типы `ISSUE_*`).

---

## 26. Файлы (создаются/изменяются)

**Изменяются — `apps/inventory`:**
- `models.py` — `MovementType += ISSUE_ITEM, ISSUE_LOT`.
- `migrations/0006_alter_stockmovement_movement_type.py`.
- `services.py` — приватные `_consume_part_item`/`_consume_stock_lot`; `sell_*`
  переписаны как обёртки; новые `issue_part_item`/`issue_stock_lot`.

**Изменяются — `apps/sales`:**
- `services.py` — public-обёртки `is_part_item_reserved`/`active_reserved_for_lot`.

**Создаются — `apps/repairs/`:**
- `__init__.py`, `apps.py`, `models.py` (`RepairOrder`, `RepairIssueLine`),
  `services.py`, `forms.py`, `views.py`, `urls.py`, `admin.py`.
- `migrations/__init__.py`, `migrations/0001_initial.py`,
  `migrations/0002_seed_repair_sequence.py` (ключ `repair_order`, `R-`).

**Изменяются — `apps/accounts`:**
- `roles.py` — `MANAGE_REPAIRS` + привязка ролей.
- `models.py` — `can_manage_repairs`.
- `context_processors.py` — пункт «Ремонт».

**Изменяются — прочее:**
- `config/settings/base.py` — `LOCAL_APPS += "apps.repairs"`.
- `config/urls.py` — `path("repairs/", include("apps.repairs.urls"))`.
- `apps/core/views.py` + `templates/core/search.html` — действие «Выдать в ремонт».

**Создаются — шаблоны `templates/repairs/`:**
- `repair_order_list.html`, `repair_order_detail.html`, `repair_order_form.html`.

**Тесты:** `tests/test_repairs.py`.

**Без изменений:** `PartItem.Status.INSTALLED` и `StockLot.Status.DEPLETED` (уже
есть → без модельных правок этих моделей); провайдер reserved Слоя 15.

---

## 27. Что будет закоммичено

Два коммита (как в Слоях 5–16):
1. `План Слоя 17: выдача в ремонт / установка` — этот файл (push в `origin/main`).
2. `Слой 17: выдача в ремонт / установка` — реализация (после `pytest`, `ruff`,
   `djlint`, `makemigrations --check`, `manage.py check`), затем **push в
   `origin/main`**.

Останавливаемся перед **Слоем 18**.

---

## Границы Слоя 17 (чего НЕ делаем)

- Не реализуем оплату, кассу, чеки, продажу, возвраты, сторно, гарантию, списания
  вне ремонтной выдачи, инвентаризацию, аналитику, PDF, CRM.
- Не делаем сложный заказ-наряд автосервиса (нормы времени/услуги/работы).
- Не делаем FIFO-автоподбор `PartType`.
- Не используем `quantity_in_repair` (final-расход; временная выдача — будущее).
- **Не пишем `StockMovement`/`StockBalance`/`PartItem.status`/`StockLot.quantity`
  напрямую из `apps/repairs`** — только через `inventory.issue_*`.
- Не превращаем `StockBalance` в источник истины.

---

## Решения (утверждены 2026-06-26)

Все рекомендации приняты заказчиком. Вопросы закрыты:

1. **Имя приложения:** `apps/repairs` — ✅ принято (не `sales`/`service`/`workshop`:
   отдельный ремонтный поток, не чистая продажа и не core).
2. **Тип движения:** `ISSUE_ITEM`/`ISSUE_LOT` — ✅ принято (общее «выдано со склада
   в работу»; установка — бизнес-контекст документа, не движения).
3. **Статус экземпляра:** `installed` — ✅ принято; `current_location` оставляем как
   последнюю складскую ячейку (аудит).
4. **Философия:** окончательный складской расход; `quantity_in_repair` не
   используем (остаётся 0) — ✅ принято. Возврат неиспользованной детали — будущий
   отдельный слой.
5. **Техника клиента:** `vehicle_type` FK (nullable) + `vehicle_make`/`vehicle_model`/
   `vehicle_identifier` текстом — ✅ принято (гибрид, без CRM/автопарка).
6. **Helper в inventory:** общий приватный `_consume_*`, `sell_*`/`issue_*` —
   тонкие обёртки — ✅ принято при условии: продажи Слоя 16 не ломаются, все
   `tests/test_sales.py` зелёные; если рефактор расползается — `issue_*` отдельно с
   минимальным дублированием.
7. **Связь с резервом:** ремонт **не** из резерва; нельзя выдать зарезервированное;
   связь с `Reservation` не делаем — ✅ принято (выдача из резерва — будущее).
8. **Право:** `MANAGE_REPAIRS` для Админ/Руководитель/Кладовщик/Продавец-Мастер;
   Наблюдатель — только просмотр; себестоимость — под `can_view_purchase_cost` —
   ✅ принято.
9. **Момент выдачи:** при проведении заказа `draft → completed`; на `complete`
   сервер заново проверяет доступность каждой строки, иначе падает с понятной
   ошибкой — ✅ принято.
10. **Номер:** `R-000001` (ASCII, как `S-`) — ✅ принято.
