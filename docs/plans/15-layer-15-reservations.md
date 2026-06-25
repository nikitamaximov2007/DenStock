# План реализации — Слой 15. Резервы (коммерческая бронь детали)

**Статус:** на согласовании (2026-06-25) · **Код не пишется до утверждения.**

---

## 1. Цель слоя

Дать возможность **зарезервировать доступную деталь для клиента**: после брони
деталь больше **не считается свободно доступной для продажи**, но **физически
остаётся на складе в той же ячейке**. Это подготовка к будущему слою продаж.

**Главная мысль (не смешивать резерв и продажу):** резерв — это «отложили для
клиента», а не «продали». Резерв **уменьшает доступность** (`available`), но **не
меняет физический остаток** (`physical`), **не создаёт `StockMovement`** и **не
двигает деталь**. Продажа будет отдельным следующим слоем (Слой 16).

**Главный архитектурный контроль (как в Слоях 10/12/14):**

- Источник истины о брони — **`Reservation` / `ReservationLine`** (новые модели).
- `StockBalance` остаётся **кэшем** (не источник истины); `quantity_reserved` /
  `quantity_available` пересобираются из первички.
- Любое изменение брони идёт **только через сервисы `apps/sales`**; вьюхи —
  оркестраторы. Никакой записи `StockMovement` при резерве.

### Что уже есть (переиспользуем)

| Уже реализовано | Где | Роль в Слое 15 |
|---|---|---|
| `PartItem.Status.RESERVED = "reserved"` (значение enum) | `apps/inventory/models.py` | **зарезервировано на будущее**; в Слое 15 статус экземпляра **намеренно не используем** (см. §7) |
| `StockBalance.quantity_reserved` (поле уже есть, всегда `0`) | `apps/inventory/models.py` | Слой 15 **наполняет** это поле через пересчёт |
| `_compute_balance` / `_refresh_balance` / `rebuild_stock_balance` / `check_stock_balance` | `apps/inventory/services.py` | расширяем «резервом» через хук-провайдер (см. §6), **без переписывания** |
| Роли/возможности (`roles.py`, `User.has_capability`) | `apps/accounts` | добавляем возможность `manage_reservations` |
| Быстрый поиск `search_parts` + `PartSearchRow` | `apps/core/search.py` | добавляем показатель `reserved` (из кэша) |
| `NumberSequence.next(key)` | `apps/inventory/models.py` | человекочитаемый номер резерва `РЕЗ-000001` |

### Что нового в Слое 15

- **Новое приложение `apps/sales`** с моделями `Reservation`, `ReservationLine`,
  сервисами, вьюхами, шаблонами, management-командой `expire_reservations`.
- **Расширение `apps/inventory/services.py`**: «хук-провайдер резерва» +
  субтракция `reserved` в `_compute_balance` (**без миграций инвентаря** — поле
  `quantity_reserved` уже есть).
- **Возможность `manage_reservations`** в `apps/accounts`.
- **Показатель `reserved`** в быстром поиске и (по месту) в карточках.

**Чего слой НЕ делает:** продажи, оплата, чеки/документы продажи, возвраты,
установки, списания, инвентаризация, аналитика продаж, PDF-этикетки, сложный CRM;
**не создаёт `StockMovement` при резерве**; **не меняет physical quantity**; **не
превращает `StockBalance` в источник истины**.

---

## 2. Где размещать резервы — **рекомендация: новое приложение `apps/sales`**

| Вариант | Плюсы | Минусы | Вывод |
|---|---|---|---|
| **`apps/sales` (рекомендация)** | дальше идут продажи, возвраты, установки — им нужен дом; `inventory` остаётся «про физический остаток»; направление зависимостей `sales → inventory` правильное; в навигации уже есть заглушка «Продажа» для продавца | нужен новый app + аккуратная развязка зависимостей (см. §6) | **выбираем** |
| `apps/inventory` | нет новой зависимости; `_compute_balance` видит резерв напрямую | втягивает коммерческие понятия (клиент, бронь, будущий `Sale`/`Customer`) в склад; будущий FK `Reservation → Customer` потянул бы инвертированную зависимость | нет |
| `apps/core` | рядом со сканером/поиском | `core` — это UI-склейка, не владелец доменных моделей | нет |

**Обоснование.** Бронь — коммерческое намерение, а не физическое состояние склада.
Следующие 3–4 слоя (продажи/возвраты/установки) коммерческие — логично завести
`apps/sales` сейчас и поселить туда `Reservation`. `apps/inventory` остаётся
сфокусированным на физическом остатке.

**Ключевой нюанс зависимостей.** `apps/sales` импортирует `apps/inventory` (FK на
`PartItem`/`StockLot`, вызов сервисов). **`apps/inventory` НЕ импортирует
`apps/sales`** — иначе цикл. Развязка — в §6 (хук-провайдер резерва).

**Имя app:** `sales` (совпадает с навигационной заглушкой «Продажа» и
коммерческим словарём роадмапа) против `commerce` — вынесено в открытые вопросы.

---

## 3. Сущности

Две модели в `apps/sales/models.py`. **Клиент — простым текстом** (не модель
`Customer`): отдельная модель с дедупликацией/историей — это CRM, она уместна в
слое продаж, а не здесь; на Слое 15 хватает пары `CharField` (обоснование §3.3).

### 3.1 `Reservation` (шапка брони)

| Поле | Тип | Назначение |
|---|---|---|
| `number` | `CharField` unique, editable=False (`NumberSequence.next("reservation")` → `РЕЗ-000001`) | человекочитаемый номер (как у `PartItem`) |
| `status` | `CharField(choices=Status)` | `draft`/`active`/`canceled`/`expired` (+ future `converted_to_sale`, см. §5) |
| `customer_name` | `CharField` | имя клиента (обязательно) |
| `customer_contact` | `CharField` blank | телефон/контакт (необязательно) |
| `comment` | `CharField` blank | примечание |
| `expires_at` | `DateTimeField` null/blank | срок брони (необязательный, §11) |
| `created_by` | FK user, `SET_NULL` | кто создал |
| `created_at` / `updated_at` | auto | аудит |
| `canceled_at` | `DateTimeField` null/blank | момент отмены/истечения |

`completed_at` / конверсия в продажу — **не здесь** (это Слой 16); чтобы не плодить
будущую миграцию, значение enum `converted_to_sale` определяем сразу, но **не
выставляем** в Слое 15.

### 3.2 `ReservationLine` (строка брони — одна деталь/лот)

Бронь содержит **несколько строк** (разные детали для одного клиента), поэтому
строки выделены в отдельную модель. XOR-паттерн повторяет `StockMovement`.

| Поле | Тип | Назначение |
|---|---|---|
| `reservation` | FK `Reservation`, `CASCADE`, related_name="lines" | шапка |
| `part_type` | FK `catalog.PartType`, `PROTECT` | денормализация для выборок/поиска |
| `part_item` | FK `inventory.PartItem`, `PROTECT`, null | **поштучный** резерв (XOR) |
| `stock_lot` | FK `inventory.StockLot`, `PROTECT`, null | **количественный** резерв (XOR) |
| `quantity` | `Decimal(12,3)` | `1` для экземпляра; дробное для лота |
| `created_at` | auto | аудит |

**Ограничения БД (как у `StockMovement`):**

```python
CheckConstraint(  # ровно один из part_item / stock_lot
    Q(part_item__isnull=False, stock_lot__isnull=True)
    | Q(part_item__isnull=True, stock_lot__isnull=False),
    name="reservationline_item_xor_lot",
)
CheckConstraint(Q(quantity__gt=0), name="reservationline_qty_positive")
```

«Активность» строки определяется статусом её **шапки** (`reservation.status ==
active` и бронь не истекла) — отдельного статуса на строке нет (проще, без
рассинхрона шапка/строка).

### 3.3 Почему клиент — текст, а не модель `Customer`

- Цель слоя — **бронь остатка**, а не управление клиентской базой.
- Реальный `Customer` (телефон как ключ, дедуп, история покупок, скидки) тянет
  валидаторы/уникальность/UI — это отдельный слой продаж/CRM.
- Минимализм: две `CharField` закрывают потребность «на кого отложили». Когда
  появится `Customer` (Слой 16+), миграция `customer_name → FK` тривиальна.

---

## 4. Что можно резервировать

| Объект | Что резервируем | Как считаем доступное |
|---|---|---|
| **`PartItem`** (поштучно) | **конкретный экземпляр целиком** (`quantity = 1`) | экземпляр должен быть `available`; нельзя зарезервировать дважды (§10) |
| **`StockLot`** (количественно) | **`quantity` (Decimal 12,3), частично или целиком**, **без физического дробления лота** | `quantity ≤ lot.quantity − активный_резерв_лота`; лот `available` |
| `PartType` «зарезервировать N штук из доступных» без выбора лота | **отложено на будущее** | усложняет (нужен FEFO/выбор лотов) — см. ниже |

**Рекомендация (совпадает с ТЗ):**

- для `PartItem` резервируем **конкретный экземпляр**;
- для `StockLot` разрешаем резерв `quantity` **без дробления лота** — строка брони
  ссылается на конкретный лот, физический `lot.quantity` не меняется, «отложенное»
  количество живёт только в `ReservationLine.quantity`;
- **резерв `PartType` без выбора лота — будущее улучшение** (потребует стратегии
  выбора лотов/распределения; на этом слое не вводим).

---

## 5. Статусы резерва — минимальный набор

```python
class Status(models.TextChoices):
    DRAFT     = "draft",     "Черновик"      # собираем строки; остаток НЕ держим
    ACTIVE    = "active",    "Активен"        # бронь держит остаток (уменьшает available)
    CANCELED  = "canceled",  "Отменён"        # освобождён вручную
    EXPIRED   = "expired",   "Просрочен"      # освобождён по сроку (команда §16)
    CONVERTED = "converted_to_sale", "Продан"  # FUTURE (Слой 16); в Слое 15 не выставляется
```

**Обоснование минимума:**

- **`draft`** нужен, потому что ТЗ требует «нельзя активировать **пустой** резерв»
  → значит есть фаза сборки строк **до** удержания остатка. В `draft` бронь **не
  влияет на `available`** (другой продавец ещё может успеть). Удержание начинается
  только при `active`.
- **`active`** — единственный статус, который **уменьшает `available`**.
- **`canceled`** и **`expired`** — два способа освободить бронь (вручную / по
  сроку); различаем их, чтобы в истории было видно причину.
- **`converted_to_sale`** определяем сразу как future-choice (чтобы не плодить
  миграцию позже), но **в Слое 15 не используем** — конверсию сделает Слой 16.

> Компромисс `draft`: пока бронь в черновике, остаток формально свободен (мягкого
> hold нет). Это осознанное упрощение v1; «мягкий hold на draft» — кандидат на
> будущее (открытый вопрос).

---

## 6. Влияние на `StockBalance` — кэш, наполняемый через хук-провайдер

**Принцип (не нарушаем):** `StockBalance` — **кэш**, источник истины — первичка
(`StockLot`/`PartItem`) **плюс** `ReservationLine`. Поле `quantity_reserved` уже
существует; Слой 15 его наполняет, а формула становится:

```
quantity_available = quantity_physical − quantity_quarantine − quantity_reserved
```

### Проблема развязки и её решение

`_compute_balance(batch_line, location)` живёт в `apps/inventory` и **не должен
импортировать `apps/sales`**. Но ему нужна цифра `reserved` по `(строка партии,
ячейка)`. Более того: **любая** инвентарная операция (`move_*`/`receive_*`/
`adjust_*`) вызывает `_refresh_balance` и иначе **затёрла бы** `reserved` нулём.

**Решение — зарегистрированный хук-провайдер (инверсия зависимости):**

```python
# apps/inventory/services.py  (НЕ импортирует sales)
_reserved_provider = None  # callable(batch_line, location) -> Decimal

def set_reserved_provider(fn):       # sales регистрирует свой расчёт в AppConfig.ready()
    global _reserved_provider
    _reserved_provider = fn

def _reserved_for(batch_line, location) -> Decimal:
    if _reserved_provider is None:   # инвентарь без sales работает как раньше (reserved = 0)
        return Decimal("0")
    return _reserved_provider(batch_line, location)
```

```python
# apps/sales/apps.py
class SalesConfig(AppConfig):
    name = "apps.sales"
    def ready(self):
        from apps.inventory.services import set_reserved_provider
        from .services import reserved_for
        set_reserved_provider(reserved_for)   # sales → inventory, один раз при старте
```

- `_compute_balance` теперь вычитает `reserved = _reserved_for(line, loc)`;
  возвращает и кладёт его в `quantity_reserved`, а `available` считает по формуле
  выше. **Серийный и количественный путь — единообразны**: всю асимметрию
  (считать строки vs суммировать `quantity`) инкапсулирует `reserved_for` в sales.
- **Затирания нет:** любой `_refresh_balance` (из move/receive/adjust/rebuild)
  спрашивает у хука актуальный резерв → строка кэша всегда корректна.
- **`rebuild_stock_balance()` не меняет сигнатуру** — пересобирает `reserved`
  автоматически через хук. Отдельный `rebuild_reserved_balance` **не нужен**.
- **`check_stock_balance()`** расширяем: `_compute_balance` начинает возвращать
  `reserved`, и сверка сравнивает также `quantity_reserved`.
- Публичная обёртка `recompute_balance_row(batch_line, location)` (тонкая над
  `_refresh_balance`) — её вызывает sales после изменения брони, **не трогая**
  приватные функции напрямую.

**`reserved_for` в `apps/sales/services.py`** (источник истины — `ReservationLine`):

```python
def reserved_for(batch_line, location) -> Decimal:
    """Активный резерв по (строка партии × ячейка). Чистая read-функция."""
    active = Q(reservation__status=Reservation.Status.ACTIVE) & (
        Q(reservation__expires_at__isnull=True) | Q(reservation__expires_at__gt=now())
    )
    serial = ReservationLine.objects.filter(
        active, part_item__batch_line=batch_line, part_item__current_location=location,
    ).aggregate(s=Sum("quantity"))["s"] or Decimal("0")
    bulk = ReservationLine.objects.filter(
        active, stock_lot__batch_line=batch_line, stock_lot__location=location,
    ).aggregate(s=Sum("quantity"))["s"] or Decimal("0")
    return serial + bulk
```

> **Почему хук, а не «передавать reserved параметром» или «двойной rebuild»:**
> явный параметр не спасает от затирания при `move_*` (инвентарь не знает резерва
> в момент перемещения); отдельный overlay-rebuild может разойтись с основным.
> Хук — единственная точка, дающая корректный `reserved` при **каждом** пересчёте.
> Альтернативы вынесены в открытые вопросы.

---

## 7. Влияние на `PartItem` — **рекомендация: статус не меняем**

**Выбор: `PartItem.status` остаётся `available`; факт брони хранится только в
`ReservationLine`; «зарезервировано» показываем запросами.**

**Обоснование:**

- **Нет рассинхрона.** Единственный источник истины о брони — `ReservationLine`.
  Если дублировать факт в `PartItem.status=reserved`, появляются две правды,
  которые надо синхронизировать в одной транзакции и сверять командой.
- **Не ломаем инвариант движений.** Документация `StockMovement` гласит: смена
  `PartItem.status` идёт через сервис движения. Резерв же **намеренно без
  движения** (§8). Перевод в `reserved` без движения создал бы статус-переход в
  обход ledger — нежелательное исключение.
- **Физический остаток не «уезжает».** Раз экземпляр остаётся `available`, он
  по-прежнему в `ITEM_PHYSICAL_STATUSES` → `physical` корректен без правок констант.
- ТЗ §7 прямо допускает этот выбор: «если это создаёт риск рассинхрона, лучше
  хранить только в `ReservationLine` и показывать reserved через запросы».

**Как показываем «зарезервировано» (без статуса):**

- в `StockBalance.quantity_reserved` (кэш, §6) — для поиска/остатков;
- в карточке экземпляра — запросом активной `ReservationLine` (вьюха `apps/sales`
  или presentation-слой может импортировать sales; **доменный `inventory.services`
  — не может**).

**Альтернатива (design B, в открытых вопросах):** выставлять
`PartItem.status=reserved` ради читаемости складских списков. Тогда дополнительно:
добавить `RESERVED` в `ITEM_PHYSICAL_STATUSES`, ввести inventory-сервис «смена
статуса без движения», добавить `check`-сверку статус ↔ строка. Не рекомендуется
(рассинхрон + исключение в ledger). Enum-значение `RESERVED` при этом остаётся
зарезервированным под будущий «жёсткий» хард-резерв/pending-продажу.

---

## 8. Влияние на `StockLot`

- **Не меняем `physical quantity`** (`lot.quantity` неизменно).
- **Не меняем `status`** лота (никакого `reserved`-статуса у лота нет и не вводим —
  лот может быть зарезервирован частично, это не свойство всего лота).
- Зарезервированное количество хранится в `ReservationLine.quantity` (ссылка на
  конкретный лот).
- Доступное по лоту: `lot.quantity − активный_резерв_этого_лота` (для проверок и
  для `available` в кэше).
- **Не создаём `StockMovement`** — физического движения нет.

---

## 9. Сервисы (`apps/sales/services.py`) — все изменения только через сервисы

```python
class ReservationError(Exception): ...

create_reservation(*, customer_name, customer_contact="", comment="",
                   expires_at=None, by) -> Reservation        # статус draft

add_part_item_to_reservation(reservation, item, *, by) -> ReservationLine
add_stock_lot_to_reservation(reservation, lot, quantity, *, by) -> ReservationLine
remove_reservation_line(line, *, by)                          # снять строку (draft/active)

activate_reservation(reservation, *, by)                      # draft → active (держит остаток)
cancel_reservation(reservation, *, by, reason="")             # → canceled, освобождает
expire_reservations(*, now=None, by=None) -> int              # active → expired по сроку

reserved_for(batch_line, location) -> Decimal                 # чистый провайдер для §6
```

**Контракты:**

- Все мутации — в `@transaction.atomic`, с `select_for_update` на `PartItem` /
  `StockLot` (§15).
- После любого изменения активной брони — **пересчёт затронутых строк кэша** через
  `inventory.recompute_balance_row(batch_line, location)`. **`StockMovement` не
  создаётся** ни в одном из сервисов резерва.
- Добавление строк допускается в `draft` **и** в `active` (в `active` — сразу
  держит остаток, пересчёт кэша на месте). Валидация доступности — §10.
- `activate_reservation` блокирует объекты всех строк, проверяет доступность
  **каждой** строки атомарно, затем переводит шапку в `active` и пересчитывает кэш.
- `cancel_reservation` / `expire_reservations` снимают активность → пересчёт кэша
  освобождает `available`; ставят `canceled_at`.
- **Вьюхи сервисную логику не дублируют** и сами в `StockBalance`/`StockMovement`
  не пишут (контроль — тест-мок §17).

---

## 10. Инварианты (и кто их гарантирует)

| Инвариант | Гарант |
|---|---|
| Нельзя резервировать `written_off`/`depleted`/`sold`/`installed`/`returned`/`quarantine` | сервис: только `available` экземпляр / `available` лот |
| Нельзя резервировать `receiving` | сервис: статус `available` обязателен |
| Нельзя резервировать больше доступного количества | сервис: `quantity ≤ lot.quantity − активный_резерв_лота` под блокировкой |
| Нельзя дважды зарезервировать один `PartItem` | сервис: под `select_for_update` проверяем отсутствие активной строки на этот экземпляр |
| Нельзя зарезервировать `StockLot` больше `lot.quantity − активный_резерв` | сервис под блокировкой лота |
| Нельзя активировать пустой резерв | `activate_reservation`: требует ≥ 1 строки |
| Отмена/истечение освобождает `available` | сервис: пересчёт кэша через `recompute_balance_row` |
| Резерв **не создаёт `StockMovement`** | сервисы резерва не вызывают `_record_movement` (тест §17) |
| Резерв **не меняет physical quantity** | сервисы не трогают `lot.quantity` / не двигают экземпляр (тест §17) |
| XOR(экземпляр, лот) и `quantity > 0` в строке | `CheckConstraint` БД (§3.2) |

«Дважды зарезервировать `PartItem`» защищаем **на уровне сервиса** под блокировкой
(а не partial-unique в БД), т.к. после `cancel`/`expire` экземпляр снова можно
бронировать — статический unique это сломал бы.

---

## 11. Срок резерва

- `expires_at` — **необязательный** `DateTimeField` на `Reservation`.
- Просроченная бронь **не считается активной**: провайдер `reserved_for` (§6) и
  все выборки фильтруют `expires_at IS NULL OR expires_at > now()` → просрочка
  **сразу перестаёт держать `available`**, даже до запуска команды.
- **Фоновую задачу/планировщик не делаем.**
- **Management-команда `expire_reservations`** (§16) переводит `active → expired`
  по сроку и пересчитывает кэш (приводит статус в соответствие, чистит «висящие»).

---

## 12. UI (`apps/sales`, шаблоны `templates/sales/`)

| Экран | URL (`name`) | Право |
|---|---|---|
| Список резервов | `/sales/reservations/` (`reservation_list`) | просмотр — все вошедшие |
| Карточка резерва | `/sales/reservations/<pk>/` (`reservation_detail`) | просмотр — все вошедшие |
| Создать резерв | `/sales/reservations/new/` (`reservation_create`) | `manage_reservations` |
| Добавить `PartItem` | POST `…/<pk>/add-item/` (`reservation_add_item`) | `manage_reservations` |
| Добавить `StockLot` (кол-во) | POST `…/<pk>/add-lot/` (`reservation_add_lot`) | `manage_reservations` |
| Снять строку | POST `…/lines/<pk>/remove/` (`reservation_remove_line`) | `manage_reservations` |
| Активировать | POST `…/<pk>/activate/` (`reservation_activate`) | `manage_reservations` |
| Отменить | POST `…/<pk>/cancel/` (`reservation_cancel`) | `manage_reservations` |

- **Карточка** показывает: номер, клиента/контакт, статус, срок, автора, строки
  (деталь, экземпляр/лот, кол-во, ячейка), кнопки активировать/отменить/снять
  строку (под правом), `messages`+PRG после действий.
- **Добавление `PartItem`** — поле для кода (скан/ввод `ITEM:`/`DS-`/серийник) или
  выбор из доступных экземпляров детали; код перепроверяется сервером.
- **Добавление `StockLot`** — выбор лота + поле количества (Decimal); проверка
  доступного остатка лота на сервере.
- **Себестоимость** строк скрыта без `can_view_purchase_cost` (контекст
  `show_costs`, как везде).
- **No-JS:** экраны server-rendered; действия — обычные `<form method="post">`.
- **Полноценную продажу не делаем** — только бронь.

Интеграция скан-резолва (`resolve_scan`) для добавления экземпляра — приятно, но
необязательно; рекомендация — форма с серверной перепроверкой, скан — опц. (откр.).

---

## 13. Интеграция с быстрым поиском (`/search/`)

- В `PartSearchRow` добавляем поле **`reserved`**.
- В кэш-ветке `search_parts` добавляем агрегат `reserved=Sum("quantity_reserved")`
  из `StockBalance` (поле уже наполнено через §6).
- **Первичный fallback** (детали **без** строк кэша): `reserved = 0`. Обоснование:
  зарезервировать можно только физически присутствующий остаток, у которого
  **есть** строки `StockBalance`, поэтому путь fallback по смыслу не содержит
  резерва. Это сохраняет `apps/core` **независимым от `apps/sales`** (core не
  импортирует sales) — той же ценой приближения, что и существующий fallback.
- В `templates/core/search.html` показываем 4 показателя:
  **всего физически / доступно / зарезервировано / на приёмке**.
- Продавец/мастер видит, что часть остатка зарезервирована, **до** попытки продажи.
- Себестоимость по-прежнему скрыта без `can_view_purchase_cost`.

---

## 14. Роли

Новая **возможность** `manage_reservations` (в `roles.py`), привязка:

| Роль | Создавать/менять резерв | Видеть резервы | Себестоимость |
|---|---|---|---|
| Администратор | ✅ | ✅ | ✅ |
| Руководитель | ✅ | ✅ | ✅ |
| **Продавец/Мастер** | ✅ | ✅ | по `can_view_purchase_cost` (нет) |
| **Кладовщик** | ❌ (только просмотр) | ✅ | нет |
| Наблюдатель | ❌ | ✅ | по `can_view_purchase_cost` |

**Обоснование «кладовщик — только просмотр».** Резерв — **коммерческое** действие
(отложить **для клиента**), это работа продавца. Кладовщик ведёт **физический**
склад (приёмка/перемещение); бронь под клиента — не его зона. Видеть брони ему
полезно (понимать, что часть остатка занята), создавать — нет. Поэтому новая
возможность, а **не** переиспользование `MANAGE_INVENTORY`/`EDIT` (которые есть у
кладовщика).

- `roles.py`: `MANAGE_RESERVATIONS = "manage_reservations"`; добавить в
  `ROLE_CAPABILITIES` для `ADMIN`, `MANAGER`, `SELLER`; в `ALL_CAPABILITIES`.
- `accounts/models.py`: свойство `can_manage_reservations`.
- **Просмотр** списка/карточки — `login_required` (все роли); **мутации** — под
  `can_manage_reservations` (403 иначе).
- Возможности вычисляются из групп в коде (`roles.py`) → **миграции не нужно**.

---

## 15. Транзакции и блокировки

- Каждая мутация брони — в `transaction.atomic`.
- `select_for_update` на `PartItem` (поштучно) и на `StockLot` (количественно) при
  добавлении строки и при активации — сериализует параллельные брони.
- **Защита от двойного резерва** `PartItem`: под блокировкой проверяем, что нет
  другой активной строки на этот экземпляр.
- **Защита от ухода `available` в минус** (лот): под блокировкой
  `новый + активный_резерв ≤ lot.quantity`.
- Тест — **последовательный** (две брони подряд: вторая упирается в лимит и
  отклоняется), проверяющий отсутствие отрицательного `available`.
- Полноценный конкурентный Postgres-тест **откладываем** (тестовая БД — SQLite, у
  которой семантика `select_for_update` ограничена); отмечаем как будущий слой.

---

## 16. Management-команды

- **`expire_reservations`** (`apps/sales/management/commands/`): `active → expired`
  где `expires_at < now`, пересчёт затронутых строк кэша. Идемпотентна, без
  планировщика (запуск вручную/по cron хоста).
- **`rebuild_stock_balance`** (существующая, инвентарь): **менять не нужно** —
  благодаря хук-провайдеру (§6) она пересобирает `reserved` автоматически.
  Отдельной `rebuild_reserved_balance` **не вводим**.
- **`check_stock_balance`** (существующая): расширяется сравнением
  `quantity_reserved` (через дополненный `_compute_balance`).
- (Опционально) seed-миграция строки `NumberSequence` для ключа `reservation`
  (`РЕЗ`) — как `0002_seed_number_sequence` у инвентаря.

---

## 17. Тесты (`tests/test_reservations.py`)

Покрываем список ТЗ:

1. Можно создать резерв (`draft`).
2. Нельзя активировать **пустой** резерв (`ReservationError`).
3. Можно зарезервировать `PartItem` (строка создана, после `activate` — держит).
4. Нельзя зарезервировать один `PartItem` дважды (вторая активная бронь отклонена).
5. Нельзя зарезервировать `receiving` `PartItem` (только `available`).
6. Можно зарезервировать `quantity` из `StockLot` (частично).
7. Нельзя зарезервировать `StockLot` больше доступного (`qty > lot.quantity −
   активный_резерв`).
8. Отмена резерва освобождает количество (`available` возвращается).
9. Резерв **не создаёт `StockMovement`** (счётчик неизменен до и после activate).
10. Резерв **не меняет physical quantity** (`lot.quantity` / число `physical`
    экземпляров неизменны).
11. `StockBalance.quantity_reserved` наполняется и `quantity_available`
    уменьшается после `activate`; восстанавливается после `cancel`/`expire`.
12. `rebuild_stock_balance` (через хук) даёт тот же `reserved`; `check_stock_balance`
    не находит расхождений.
13. Просроченная бронь (`expires_at < now`) **не держит** `available` даже до
    команды; `expire_reservations` переводит её в `expired`.
14. `/search/` показывает `reserved` (физически/доступно/зарезервировано/приёмка).
15. Продавец/Мастер **может** создать резерв (`manage_reservations`).
16. Кладовщик/Наблюдатель **не может** создавать (403), но **видит** список.
17. Себестоимость скрыта без `can_view_purchase_cost` (кладовщик не видит сумм).
18. **Архитектурный мок:** при активации вьюха вызывает сервис, а сама **не
    пишет** `StockMovement`/`StockBalance` (`patch` сервиса → счётчики ledger
    неизменны).
19. Hidden/query-параметры перепроверяются сервером: подмена `item`/`lot`/`qty`
    на негодные → ошибка, без эффекта (доверяем БД/сервису, не полям формы).
20. Инвариант XOR/`quantity>0` строки на уровне БД (нельзя создать «пустую»/двойную
    строку).

---

## 18. Ручная проверка

1. Войти продавцом → «Резервы» → «Создать»: клиент, (опц.) срок → черновик.
2. Добавить `available`-экземпляр (скан/ввод `DS-…`) и количество из лота →
   строки появились; остаток ещё свободен (бронь `draft`).
3. Активировать → в `/search/` «доступно» уменьшилось на зарезервированное,
   «зарезервировано» выросло; «физически» **не изменилось**; «на приёмке» — без
   изменений.
4. В «Движениях» (Слой 10) **новых записей нет** (резерв не создаёт движение).
5. Попробовать зарезервировать тот же экземпляр во второй брони → отказ.
6. Попробовать лот больше доступного → отказ с понятным текстом.
7. Отменить бронь → «доступно» вернулось; «зарезервировано» обнулилось.
8. Поставить `expires_at` в прошлое → в `/search/` «доступно» сразу как без брони;
   `python manage.py expire_reservations` → статус `expired`.
9. `python manage.py rebuild_stock_balance` и `check_stock_balance` →
   расхождений нет (reserved пересобран корректно).
10. Войти кладовщиком → «Резервы» виден, кнопок создания/действий нет; прямой POST
    активации → 403. Войти продавцом без `can_view_purchase_cost` → сумм нет.

---

## 19. Критерии готовности

1. Можно создать бронь, добавить `PartItem`/`StockLot`-кол-во, активировать,
   отменить — **только через сервисы `apps/sales`**; вьюхи `StockMovement`/
   `StockBalance` не пишут (доказано мок-тестом §17.18).
2. Активная бронь **уменьшает `available`** и **наполняет `quantity_reserved`**, но
   **не меняет `physical`** и **не создаёт `StockMovement`** (§17.9–17.11).
3. `StockBalance` остаётся **кэшем**: `reserved`/`available` пересобираются
   `rebuild_stock_balance` (через хук) и сходятся в `check_stock_balance` (§17.12).
4. Инварианты §10 соблюдены (нельзя бронировать недоступное/`receiving`/дважды;
   нельзя превысить остаток; нельзя активировать пустой; отмена освобождает).
5. Срок: просрочка не держит `available`; `expire_reservations` переводит в
   `expired`; планировщика нет (§11).
6. Поиск показывает 4 показателя (физически/доступно/зарезервировано/приёмка);
   себестоимость скрыта без `can_view_purchase_cost` (§13).
7. Права: создание/мутации — `manage_reservations` (Админ/Руководитель/Продавец);
   кладовщик/наблюдатель — просмотр; продавец без права суммы — не видит (§14).
8. `apps/inventory` **не импортирует `apps/sales`** (развязка через хук §6);
   `apps/core` не импортирует `apps/sales` (§13).
9. Границы соблюдены: нет продаж/оплат/чеков/возвратов/установок/списаний/
   инвентаризации/аналитики/PDF/CRM; резерв не создаёт движение; physical не
   меняется; `StockBalance` не стал источником истины.
10. Тесты зелёные; `ruff`/`djlint` чисты; `manage.py check` ок; `makemigrations
    --check` — миграции **только** в `apps/sales` (инвентарь без миграций).

---

## 20. Файлы (создаются/изменяются)

**Создаются — `apps/sales/`:**
- `__init__.py`, `apps.py` (`ready()` регистрирует `reserved_for`), `models.py`
  (`Reservation`, `ReservationLine`), `services.py`, `forms.py`, `views.py`,
  `urls.py`, `admin.py` (опц.).
- `migrations/__init__.py`, `migrations/0001_initial.py`
  (+ опц. `0002_seed_number_sequence` для ключа `reservation`).
- `management/__init__.py`, `management/commands/__init__.py`,
  `management/commands/expire_reservations.py`.

**Создаются — шаблоны `templates/sales/`:**
- `reservation_list.html`, `reservation_detail.html`, `reservation_form.html`
  (+ partial-формы добавления строки, если удобно).

**Изменяются:**
- `config/settings/base.py` — `LOCAL_APPS += "apps.sales"`.
- `config/urls.py` — `path("sales/", include("apps.sales.urls"))`.
- `apps/accounts/roles.py` — `MANAGE_RESERVATIONS` + привязка к ролям.
- `apps/accounts/models.py` — свойство `can_manage_reservations`.
- `apps/accounts/context_processors.py` — пункт «Резервы» (просмотр — всем
  вошедшим; ставим вместо/рядом с заглушкой «Продажа»).
- `apps/inventory/services.py` — `set_reserved_provider`/`_reserved_for`,
  субтракция `reserved` в `_compute_balance` (+ возврат `reserved`),
  `recompute_balance_row`, учёт `reserved` в `check_stock_balance`. **Без миграций.**
- `apps/core/search.py` — поле `reserved` в `PartSearchRow` + агрегат из кэша.
- `templates/core/search.html` — колонка «Зарезервировано».
- `templates/inventory/item_detail.html`, `lot_detail.html` — показ «зарезервировано»
  / ссылка «Зарезервировать» (по месту; опц.).

**Тесты:** `tests/test_reservations.py` (+ при необходимости правки `test_search.py`).

**Без изменений:** модели `apps/inventory` (поле `quantity_reserved` и enum
`RESERVED` уже есть → **миграций инвентаря нет**); `StockMovement` (резерв движений
не создаёт); `resolve_scan` (остаётся чистым).

---

## 21. Что будет закоммичено

Два коммита (как в Слоях 5–14):
1. `План Слоя 15: резервы` — этот файл.
2. `Слой 15: резервы` — реализация (после `pytest`, `ruff`, `djlint`,
   `makemigrations --check`, `manage.py check`), затем **push в `origin/main`**.

Останавливаемся перед **Слоем 16 (продажи)**.

---

## Границы Слоя 15 (чего НЕ делаем)

- Не реализуем продажи, оплату, чеки/документы продажи, возвраты, установки,
  списания, инвентаризацию, аналитику продаж, PDF-этикетки.
- **Не создаём `StockMovement` при резерве.**
- **Не меняем physical quantity** при резерве (лот не дробим, экземпляр не двигаем).
- **Не превращаем `StockBalance` в источник истины** (он кэш; истина —
  `ReservationLine`).
- **Не делаем сложный CRM** (клиент — текст, не модель `Customer`).
- **Не вводим резерв `PartType` без выбора лота** (будущее улучшение).
- **Не меняем `PartItem.status`** на `reserved` (рекомендация §7; design B — откр.).
- `apps/inventory` **не импортирует** `apps/sales`.

---

## Открытые вопросы на согласование

1. **Имя приложения:** `apps/sales` (рекомендация, совпадает с заглушкой «Продажа»)
   против `apps/commerce`. Ок `sales`?
2. **`PartItem.status`:** не менять, бронь только в `ReservationLine` (design A,
   рекомендация) против выставлять `status=reserved` (design B: +`ITEM_PHYSICAL_
   STATUSES`, +сервис «статус без движения», +сверка). Ок design A?
3. **Механизм `reserved` в кэше:** хук-провайдер (рекомендация — не затирается при
   `move_*`) против явного параметра / отдельного `rebuild_reserved_balance`. Ок хук?
4. **Клиент:** текстовые поля (рекомендация) против модели `Customer`. Ок текст?
5. **`draft`:** черновик без удержания остатка (рекомендация) против «создавать
   сразу active одним шагом». Нужен ли «мягкий hold на draft» (будущее)? Ок draft?
6. **Номер брони:** `РЕЗ-000001` через `NumberSequence` (рекомендация, как у
   экземпляров; нужна seed-миграция ключа) против просто `pk`. Ок `NumberSequence`?
7. **Добавление экземпляра:** форма с серверной перепроверкой (рекомендация) против
   полноценного скан-резолва на экране резерва. Ок форма (скан — опц.)?
8. **`reserved` в fallback поиска:** считать `0` для деталей без кэша (рекомендация,
   сохраняет `core` независимым от `sales`) против точного расчёта (потянет
   зависимость `core → sales`). Ок `0` в fallback?
9. **Резерв `StockLot`:** строка ссылается на **конкретный лот** + `quantity`
   (рекомендация) против резерва по `(batch_line, location)` без выбора лота. Ок лот?
