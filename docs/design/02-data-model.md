# Раздел 2. Модель данных (переработанная)

**Статус:** на согласовании (2026-06-24)
**Учтены правки заказчика 1–7** (см. карту правок в конце документа).

Условные обозначения: `FK` — ссылка на другую сущность, `M2M` — многие-ко-многим,
`uniq` — уникальность, `idx` — индекс, `✓null` — поле может быть пустым.

---

## 2.1. Ключевая идея разделения (правки 1 и 2)

Чётко разводим три разных понятия, которые нельзя смешивать:

| Понятие | Сущность | Что это | Меняется? |
|---|---|---|---|
| **Что приехало** | `BatchLine` | Историческая строка поступления партии | Замораживается после фиксации себестоимости |
| **Что осталось сейчас (массовое)** | `StockLot` | Текущий остаток: деталь + партия + ячейка + кол-во | Да, через движения |
| **Что осталось сейчас (поштучно)** | `PartItem` | Один физический экземпляр со статусом и ячейкой | Да, через движения |
| **Все изменения** | `StockMovement` | Неизменяемый журнал операций | Только добавление |
| **Быстрый агрегат** | `StockBalance` | Кэш доступности по детали (денормализация) | Перестраиваемый из остатков |

- **Поштучный учёт** (двигатель, редуктор, блок управления): одна строка
  `PartItem` = один физический экземпляр.
- **Количественный учёт** (болты, фильтры, прокладки): одна строка `StockLot`
  = партия + ячейка + количество.

Это разные правила и разные таблицы — универсальной «одной таблицы на всё» нет.

---

## 2.2. Каталог (`catalog`)

### Category — категория (дерево)
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| parent | FK→Category ✓null | дерево; `idx` |
| name | char | |
| sort_order | int | |

Инвариант: запрет циклов (проверка при сохранении).

### EquipmentType — вид техники
`id`, `name` (uniq). Расширяется администратором (авто, снегоход, квадроцикл, катер, яхта, …).

### Manufacturer — производитель детали
`id`, `name` (uniq), `country` ✓null.

### Brand — марка техники
`id`, `name` (uniq).

### VehicleModel — модель техники
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| brand | FK→Brand | |
| equipment_type | FK→EquipmentType | |
| name | char | |
| year_from / year_to | int ✓null | годы выпуска |

`uniq(brand, name, year_from, year_to)`.

### PartType — карточка вида детали
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| name | char | `idx` (триграммный для поиска по части названия) |
| category | FK→Category | |
| manufacturer | FK→Manufacturer ✓null | |
| description | text | |
| accounting_mode | enum | `SERIAL` (поштучно) / `BULK` (количественно) |
| unit | FK→Unit | единица измерения |
| primary_oem | char | `idx` |
| min_stock | decimal | минимальный остаток |
| recommended_price | decimal ✓null | **рекомендуемая цена** (правка 5) |
| min_price | decimal ✓null | **минимальная цена продажи** (правка 5) |
| note | text | |
| is_active | bool | |
| internal_code | char ✓null uniq | внутренний код самой карточки |
| created_at / updated_at | datetime | |

### PartNumber — номера детали
`id`, `part` FK→PartType, `value` char `idx`, `kind` enum (`OEM`/`ARTICLE`/`ANALOG`),
`manufacturer` ✓null, `note`. Одна деталь — много номеров и аналогов.

### PartBarcode — заводские штрихкоды
`id`, `part` FK→PartType, `value` char `idx`, `meaning` enum
(`TYPE`/`PACKAGE`/`INSTANCE`/`SERIAL`).

### PartPhoto — фотографии
`id`, `part` FK→PartType, `image`, `is_primary` bool, `sort_order`.

### PartCompatibility — совместимость с техникой
`id`, `part` FK→PartType, `vehicle_model` FK→VehicleModel, `note`.
`uniq(part, vehicle_model)`. Покрывает «вид техники / марка / модель / годы».

### PartAnalog — аналоги-детали
`id`, `part` FK→PartType, `analog_part` FK→PartType. Аналоги внутри каталога
(плюс свободные аналоги через `PartNumber.kind=ANALOG`).

### Unit — единица измерения
`id`, `name` (шт, компл, м, кг, л), `short_name`.

---

## 2.3. Поставщики (`suppliers`)

### Supplier
`id`, `name` (uniq), `country`, `contact_person`, `phone`, `email`, `website`,
`currency` (код, по умолч. `RUB`), `comment`. История поставок выводится из партий.

---

## 2.4. Хранение (`warehouse`)

### StorageLocation — место хранения (self-referential дерево)
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| parent | FK→StorageLocation ✓null | дерево; `idx` |
| kind | enum | `WAREHOUSE`/`ZONE`/`RACK`/`SECTION`/`SHELF`/`CELL` |
| name | char | |
| code | char uniq | уникальный код |
| barcode | char uniq ✓null | штрихкод места |
| description | text | |
| status | enum | `ACTIVE`/`BLOCKED` |
| capacity | decimal ✓null | вместимость (при необходимости) |
| **storage_allowed** | bool | **можно ли хранить детали здесь** (правка 7) |
| is_receiving | bool | зона приёмки |
| sort_order | int | |

- Полный адрес (`СКЛАД-1 / A / 03 / 02 / 04`) собирается по цепочке `parent`.
- **Инвариант (правка 7):** `PartItem.location` и `StockLot.location` обязаны
  ссылаться на место с `storage_allowed = true`. Проверка на уровне модели и
  сервисного слоя; запрет хранения в неконечных/служебных узлах.

---

## 2.5. Снабжение (`procurement`)

### Batch — партия
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| internal_number | char uniq | `П-000123` |
| supplier | FK→Supplier | |
| country | char | |
| order_number / invoice_number | char ✓null | |
| order_date / ship_date / arrival_date | date ✓null | |
| currency | char | по умолч. `RUB` |
| exchange_rate | decimal | курс к рублю |
| goods_cost | decimal | стоимость товаров (в валюте) |
| shipping_cost / customs_cost / fees / extra_costs | decimal | доп. расходы (в валюте) |
| total_rub | decimal | = (товары+доставка+таможня+комиссии+проч.) × курс |
| overhead_rub | decimal | = (доставка+таможня+комиссии+проч.) × курс — к распределению |
| allocation_base | enum | `BY_VALUE` (по стоимости, по умолч.) / `BY_QUANTITY` |
| status | enum | см. ниже |
| cost_finalized | bool | себестоимость зафиксирована |
| comment | text | |

**Статусы партии (правка 3):**
`DRAFT` создана → `ORDERED` заказана → `IN_TRANSIT` в пути → `ARRIVED` прибыла →
`RECEIVING` принимается → `ACCEPTED` принята → `COST_CALCULATED` себестоимость
рассчитана → `CLOSED` закрыта. Отдельно `CANCELLED` отменена.

- `cost_finalized = true` при статусах `COST_CALCULATED` и `CLOSED`.
- **Инвариант (правка 3):** продажа, установка и резерв из остатков партии
  **запрещены**, пока `cost_finalized = false`. Иначе показывается:
  *«Партия ещё не закрыта. Продажа невозможна — себестоимость не зафиксирована.»*

### BatchLine — строка поступления (историческая, правка 1)
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| batch | FK→Batch | `idx` |
| part | FK→PartType | |
| quantity_received | decimal | сколько приехало |
| base_unit_cost_rub | decimal | закупочная цена за штуку в ₽ (без накладных) |
| allocated_overhead_rub | decimal | доля накладных на строку (всего) |
| landed_unit_cost_rub | decimal ✓null | **итоговая себестоимость за штуку** = base + allocated/qty |
| note | text | |

- Заполняется при поступлении (`base_unit_cost_rub`), `landed_unit_cost_rub`
  считается при переходе партии в `COST_CALCULATED`.
- **Инвариант:** строка неизменяема после `batch.cost_finalized = true`.

### BatchDocument
`id`, `batch` FK→Batch, `file`, `name`, `uploaded_at`.

---

## 2.6. Ядро учёта (`inventory`)

### PartItem — экземпляр (поштучный учёт, правка 2)
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| part | FK→PartType | только `accounting_mode=SERIAL` |
| internal_number | char uniq | `DS-00000125`; `idx` |
| internal_barcode | char uniq | `idx` |
| serial_number | char ✓null | `idx` |
| batch | FK→Batch | |
| batch_line | FK→BatchLine ✓null | |
| unit_cost_rub | decimal | **замороженная себестоимость** (landed) |
| status | enum | см. ниже |
| location | FK→StorageLocation ✓null | `storage_allowed=true`; `idx` |
| created_at / updated_at | datetime | |

Статусы: `IN_RECEIVING` на приёмке, `IN_STOCK` в наличии, `RESERVED` резерв,
`SOLD` продан, `INSTALLED` установлен, `ISSUED_TO_REPAIR` выдан в ремонт,
`WRITTEN_OFF` списан, `RETURNED` возвращён. Одна строка = один физический экземпляр (кол-во = 1).

### StockLot — остаток партии (количественный учёт, правки 1 и 2)
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| part | FK→PartType | только `accounting_mode=BULK` |
| batch | FK→Batch | |
| batch_line | FK→BatchLine | |
| location | FK→StorageLocation | `storage_allowed=true` |
| quantity | decimal | текущий физический остаток в этой ячейке; **CHECK ≥ 0** |
| unit_cost_rub | decimal | замороженная себестоимость за штуку |
| created_at / updated_at | datetime | |

`uniq(part, batch, location)`; `idx(part)`, `idx(location)`, `idx(batch)`.
Источник изменений — движения; править `quantity` напрямую нельзя.

### StockBalance — кэш доступности (денормализация)
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| part | FK→PartType uniq | |
| physical_qty | decimal | суммарно по всем партиям/ячейкам |
| reserved_qty | decimal | активные резервы |
| issued_qty | decimal | выдано в ремонт |
| available_qty | decimal | = physical − reserved − issued |

Поддерживается в той же транзакции, **полностью пересобираем** из `StockLot` +
`PartItem` + `Reservation`. Нужен только для скорости поиска и главной панели.
*Примечание для согласования:* если хотите минимум таблиц — можно считать
доступность «на лету» и от `StockBalance` отказаться. Рекомендую оставить как кэш.

### StockMovement — движение (неизменяемый журнал)
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| type | enum | `RECEIPT`/`MOVE`/`RESERVE`/`UNRESERVE`/`SALE`/`INSTALL`/`ISSUE_TO_REPAIR`/`RETURN`/`WRITEOFF`/`CORRECTION`/`INV_SURPLUS`/`INV_SHORTAGE` |
| created_at | datetime | `idx` |
| user | FK→User | |
| part | FK→PartType | `idx` |
| part_item | FK→PartItem ✓null | для поштучных; `idx` |
| batch | FK→Batch ✓null | для количественных |
| quantity | decimal | > 0 (для поштучных = 1) |
| from_location | FK→StorageLocation ✓null | |
| to_location | FK→StorageLocation ✓null | |
| unit_cost_rub | decimal ✓null | снимок себестоимости |
| document_type / document_id | generic ✓null | связь с продажей/установкой/списанием/резервом/инвентаризацией |
| comment | text | |

Только добавление: запрет update/delete. `idx(type)`, `idx(part_item)`, `idx(batch)`.

---

## 2.7. Резервы (`reservations`, правка 4)

### Reservation — резерв (отдельный бизнес-документ)
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| number | char uniq | `Р-000123` |
| customer_name | char | клиент |
| phone | char | |
| part | FK→PartType | |
| quantity | decimal | |
| part_item | FK→PartItem ✓null | конкретный экземпляр (поштучный) |
| batch | FK→Batch ✓null | конкретная партия (количественный) |
| created_at | datetime | |
| expires_at | datetime | срок действия |
| comment | text | |
| user | FK→User | |
| status | enum | `ACTIVE`/`COMPLETED_BY_SALE`/`CANCELLED`/`EXPIRED` |
| sale | FK→Sale ✓null | заполняется при преобразовании в продажу |

- Уменьшает доступный остаток; зарезервированное не показывается доступным другим.
- Преобразуется в продажу (одной кнопкой). Истёкшие по сроку освобождают остаток.
- `idx(status)`, `idx(expires_at)`, `idx(part)`.

---

## 2.8. Продажи и установки (`sales`, правка 6)

Продажа и установка — **разные документы**, но обе уменьшают остаток, фиксируют
себестоимость, цену для клиента и попадают в аналитику.

### Sale — продажа
`id`, `number` uniq (`ПР-000123`), `datetime`, `customer_name`, `phone`,
`employee` FK→User, `payment_method`, `discount_total` decimal, `total` decimal,
`comment`, `status` (`COMPLETED`/`CANCELLED`).

### SaleLine — позиция продажи
| Поле | Тип | Примечание |
|---|---|---|
| id | PK | |
| sale | FK→Sale | `idx` |
| part | FK→PartType | |
| part_item | FK→PartItem ✓null | поштучный |
| batch | FK→Batch ✓null | количественный (выбор партии — FIFO/ручной) |
| stock_lot | FK→StockLot ✓null | конкретный остаток-источник |
| quantity | decimal | |
| unit_cost_rub | decimal | **замороженная себестоимость** на момент продажи |
| unit_price | decimal | **фактическая цена продажи** |
| discount | decimal | скидка по позиции |
| line_total | decimal | сумма позиции |
| profit | decimal | = line_total − quantity × unit_cost_rub |

### Installation — установка в технику
`id`, `number` uniq (`УСТ-000123`), `datetime`, `customer_name`, `phone`,
`master` FK→User, `repaired_equipment` char (ремонтируемая техника),
`comment`, `status`, `total`.

### InstallationLine — позиция установки
Та же структура, что `SaleLine`: `part`, `part_item`/`batch`/`stock_lot`,
`quantity`, `unit_cost_rub` (замороженная), `price_charged` (цена клиенту),
`discount`, `line_total`, `profit`.

**Отмена (правки к разделу 18 ТЗ):** ни продажа, ни установка не удаляются.
Отмена создаёт обратные движения и ставит статус `CANCELLED`.

---

## 2.9. Операции (`operations`)

### WriteOff — списание
`id`, `number`, `datetime`, `user` FK, `reason` enum
(`DEFECT`/`DAMAGE`/`LOSS`/`USED`/`UNUSABLE`/`INV_SHORTAGE`/`OTHER`),
`comment` (**обязателен**), `status` (`PENDING`/`CONFIRMED`/`CANCELLED`),
`confirmed_by` FK→User ✓null.
### WriteOffLine
`writeoff` FK, `part` FK, `part_item`/`batch`/`stock_lot` ✓null, `quantity`,
`location` FK, `unit_cost_rub` (снимок).

### InventoryCount — инвентаризация
`id`, `number`, `scope_location` FK→StorageLocation, `datetime`, `user` FK,
`status` (`DRAFT`/`COUNTING`/`REVIEW`/`APPLIED`/`CANCELLED`).
### InventoryCountLine
`count` FK, `part` FK, `batch` ✓null, `location` FK, `expected_qty`,
`counted_qty`, `diff`. Применение → движения `INV_SURPLUS`/`INV_SHORTAGE`.

### Return — возврат
`id`, `number`, `datetime`, `user` FK, `sale` FK→Sale (исходная продажа),
`status`, `comment`.
### ReturnLine
`return` FK, `sale_line` FK→SaleLine, `part` FK, `quantity`,
`to_location` FK (по умолч. исходная), `restored` bool, `condition` text.
Возвращает деталь на склад с исходной партией и себестоимостью.

---

## 2.10. Журнал и служебное

### ActivityLog (`audit`)
`id`, `user` FK, `datetime` `idx`, `action_type` (вход, создание/изменение
детали, изменение стоимости, партия, поступление, перемещение, продажа,
отмена продажи, резерв, списание, инвентаризация, настройки),
`object_type`/`object_id` (generic), `old_value` JSON ✓null, `new_value` JSON ✓null,
`comment`. Дополнительно — детальная история полей (стоимость, цены, настройки).

### NumberSequence (`core`)
`name`, `prefix`, `padding`, `current_value`. Атомарная выдача внутренних
номеров (`select_for_update`) — без дублей при одновременной работе.

### AppSetting (`core`)
Синглтон: название компании, дефолт минимального остатка, префиксы нумерации,
расписание бэкапов, валюта по умолчанию (`RUB`).

### BackupLog (`core`)
`id`, `datetime`, `type` (`AUTO`/`MANUAL`), `status` (`SUCCESS`/`FAIL`),
`file_path`, `size`, `message`.

---

## 2.11. Модель цен (правка 5)

| Показатель | Где хранится |
|---|---|
| Себестоимость (landed) | `BatchLine.landed_unit_cost_rub` → заморожена в `PartItem`/`StockLot.unit_cost_rub` |
| Рекомендуемая цена | `PartType.recommended_price` |
| Минимальная цена | `PartType.min_price` |
| Фактическая цена | `SaleLine.unit_price` / `InstallationLine.price_charged` |
| Скидка | `Sale.discount_total`, `SaleLine.discount` |
| Прибыль | `SaleLine.profit` / `InstallationLine.profit` (заморожена) |

---

## 2.12. Ключевые инварианты

1. **Остаток ≥ 0 всегда.** `CHECK (quantity >= 0)` на `StockLot`; переходы
   статусов `PartItem`; в сервисном слое — `select_for_update` + проверка перед
   списанием. Одновременная продажа последней детали → честная ошибка второму.
2. **Изменение остатка только через `StockMovement`.** Прямое редактирование
   `quantity`/`status` запрещено; балансы меняются в одной транзакции с движением.
3. **`BatchLine` неизменяема** после `batch.cost_finalized = true`.
4. **Нет расхода из незафиксированной партии.** Продажа/установка/резерв
   возможны только при `cost_finalized = true` (статус `COST_CALCULATED`/`CLOSED`).
5. **Хранение только в `storage_allowed`-местах.** `PartItem.location` и
   `StockLot.location` обязаны указывать на место с `storage_allowed = true`.
6. **Внутренние номера уникальны** и выдаются атомарно (`NumberSequence`).
7. **Документы не удаляются.** Отмена продажи/установки — обратными движениями.
8. **Себестоимость заморожена** в момент расхода (копия в строке документа).
9. **Доступно = физический − активные резервы − выдано в ремонт.**

---

## 2.13. Индексы для поиска (раздел 16 ТЗ)

- PostgreSQL `pg_trgm` (GIN) на `PartType.name`, `PartNumber.value` — поиск по
  части названия/номера.
- B-tree на: `PartType.primary_oem`, `PartBarcode.value`,
  `PartItem.internal_number`/`internal_barcode`/`serial_number`,
  `StorageLocation.code`/`barcode`, `Batch.internal_number`.
- Единая точка сканера резолвит строку по порядку: внутренний номер → код места
  → серийный номер → заводской штрихкод → OEM/артикул.

---

## Карта правок заказчика

| № | Правка | Как закрыто |
|---|---|---|
| 1 | Разделить «приехало» и «остаток» | `BatchLine` (история) ≠ `StockLot`/`PartItem` (текущее) ≠ `StockBalance` (кэш) |
| 2 | Разные правила поштучного/количественного | `PartItem` (1 строка = 1 экземпляр) vs `StockLot` (партия+ячейка+кол-во) |
| 3 | Запрет продажи из незакрытой партии | Статус `COST_CALCULATED`, флаг `cost_finalized`, инвариант 4 |
| 4 | Резерв — отдельный документ | `Reservation` с номером, клиентом, сроком, статусом, преобразованием в продажу |
| 5 | Поля цен | `recommended_price`, `min_price`, `unit_cost`, `unit_price`, `discount`, `profit` (раздел 2.11) |
| 6 | Установка ≠ продажа | Отдельные `Installation`/`InstallationLine`; обе уменьшают остаток и идут в аналитику раздельно |
| 7 | Хранение только в конечных ячейках | `StorageLocation.storage_allowed`, инвариант 5 |
