# План реализации — Слой 6. Партии и строки поступления

**Статус:** на согласовании (2026-06-24) · **Код не пишется до утверждения.**

## 1. Цель слоя

Создать **документы партий** (`Batch`) и **строки поступления** (`BatchLine`) —
что заказано/приехало в партии, с базовыми суммами. **Без остатков, без landed
cost, без допуска к продаже.** Landed cost и `cost_finalized=true` — Слой 7;
физические остатки — слои 8–12.

> **Главная проверка слоя:** `BatchLine` **не становится остатком**. Это только
> строка документа партии. Остатки начинаются позже.

## 2. Django-приложение

- **`procurement`** — единственное новое приложение слоя.

## 3. Модель `Batch`

| Поле | Тип | Примечание |
|---|---|---|
| number | char(20) **uniq** | `П-000001` (генерация — п.7) |
| supplier | FK→suppliers.Supplier | `on_delete=PROTECT` |
| status | enum | см. п.4 |
| country | char(100) blank | по умолч. из поставщика |
| currency | char(3) | по умолч. `RUB` |
| exchange_rate | decimal(12,4) | курс к рублю, по умолч. 1 |
| order_number / invoice_number | char(100) blank | |
| ordered_at / shipped_at / arrived_at | date ✓null | |
| notes | text blank | |
| cost_finalized | bool | **по умолч. `false`; на этом слое всегда `false`** |
| goods_total | decimal(14,2) | сумма товаров (справочно, п.9) |
| shipping_cost / customs_cost / commission_cost / other_cost | decimal(14,2) | доп. расходы (без распределения) |
| total_extra_cost | decimal(14,2) | сумма доп. расходов (вычисляется) |
| created_by | FK→accounts.User ✓null | `on_delete=SET_NULL` |
| created_at / updated_at | datetime | |

## 4. Статусы партии

`DRAFT` создана · `ORDERED` заказана · `IN_TRANSIT` в пути · `ARRIVED` прибыла ·
`RECEIVING` принимается · `ACCEPTED` принята · `CANCELED` отменена.

- Разрешённые переходы на этом слое (вперёд по цепочке и в отмену):
  `DRAFT→ORDERED→IN_TRANSIT→ARRIVED→RECEIVING→ACCEPTED`; `CANCELED` — из ранних
  статусов (до `RECEIVING`, т.к. физического остатка ещё нет вообще).
- `COST_CALCULATED` и `CLOSED` **заложены в choices как будущие**, но переходы в
  них и расчёт landed cost на этом слое **не реализуются** (Слой 7).

## 5. Модель `BatchLine`

| Поле | Тип | Примечание |
|---|---|---|
| batch | FK→Batch | `on_delete=CASCADE`, `related_name="lines"` |
| part_type | FK→catalog.PartType | `on_delete=PROTECT` |
| quantity | decimal(12,3) | сколько заказано/приехало |
| unit_cost_currency | decimal(12,2) | цена за единицу в валюте партии |
| unit_cost_rub | decimal(12,2) | цена за единицу в ₽ (= валюта × курс) |
| total_cost_currency | decimal(14,2) | = quantity × unit_cost_currency |
| total_cost_rub | decimal(14,2) | = quantity × unit_cost_rub |
| note | char(255) blank | |
| created_at / updated_at | datetime | |

> **Соответствие `02-data-model.md`:** здешний `unit_cost_rub` — это **базовая
> закупочная цена** (только товар, без накладных) = прежний `base_unit_cost_rub`.
> Поля `allocated_overhead_rub` и `landed_unit_cost_rub` добавятся на Слое 7.

## 6. Правила (границы)

- `BatchLine` фиксирует **что заказано или приехало** в партии.
- `BatchLine` **не является остатком**.
- `BatchLine` **не создаёт** `StockLot`, `PartItem`, `StockMovement`, `StockBalance`.
- Из `BatchLine` **нельзя продавать**.
- Партия с `cost_finalized=false` **недоступна** для продажи/установки/резерва в
  будущих слоях. Сейчас продаж нет — правило **только фиксируется** в модели
  (свойство `is_available_for_sale = cost_finalized`) и в документации.

## 7. Генерация номера партии

- Формат `П-000001` (zero-padded суффикс).
- Без дублей: генерация в транзакции с блокировкой последней строки
  (`select_for_update` по максимальному номеру) и инкрементом суффикса.
- **Полностью атомарный счётчик** (`NumberSequence` из `core`, см. `02`) вводится
  позже, когда нумерация понадобится нескольким сущностям (экземпляры, продажи).
  На этом слое — корректно и без дублей в обычной работе.

## 8. Расчёт сумм строки

- `total_cost_currency = quantity × unit_cost_currency`.
- `unit_cost_rub = round(unit_cost_currency × batch.exchange_rate, 2)`.
- `total_cost_rub = quantity × unit_cost_rub` (или `total_cost_currency × курс`,
  округление до копеек).
- Только **`Decimal`** (не float), округление до копеек `ROUND_HALF_UP`.
- Считается в `BatchLine.save()` на основе курса партии.

## 9. Дополнительные расходы партии (без распределения)

Заложить поля `goods_total`, `shipping_cost`, `customs_cost`, `commission_cost`,
`other_cost`, `total_extra_cost` (вычисляется как сумма четырёх расходов).
**Не распределять** их по деталям на этом слое — это landed cost (Слой 7).

## 10. Экраны

- **Список партий** — фильтр по статусу/поставщику, поиск по номеру.
- **Создание партии** / **редактирование партии** — реквизиты и расходы.
- **Карточка партии** — реквизиты, строки, суммы, статус; кнопки управления.
- **Добавление строки** / **редактирование строки** — пока партия «открыта»
  (статусы до `ACCEPTED`, не `CANCELED`).
- **Удаление строки** — **только если партия в `DRAFT`**.
- **Смена статуса** — в разрешённых пределах (п.4).

## 11. Права доступа

Добавляю capability **`MANAGE_BATCHES`** (Администратор, Руководитель).

| Действие | Кто |
|---|---|
| Создание/редактирование партий и строк, смена статуса | `MANAGE_BATCHES` (Админ, Руковод.) |
| Просмотр партий (без сумм) | любой авторизованный |
| Просмотр **закупочных сумм** (цены, итоги, расходы, курс) | только `can_view_purchase_cost` (Админ, Руковод., Наблюдатель) |

**Обоснование по ролям:**
- **Кладовщик** видит партии (что приехало), но **без закупочных сумм**
  (`can_view_purchase_cost=false`). Право переводить партию в `receiving/accepted`
  логично дать **на Слое 12** (реальная приёмка через сканер), где это
  осмысленно; на Слое 6 управление статусом — у `MANAGE_BATCHES`, чтобы не делать
  половинчатую функцию.
- **Продавец/Мастер** — закупки не его зона; суммы скрыты
  (`can_view_purchase_cost=false`). Просмотр партий ему не нужен — в навигации не
  показываем.
- **Наблюдатель** — просмотр, включая суммы (`can_view_purchase_cost=true`), но
  без редактирования.

Закупочные суммы (`unit_cost_*`, `total_cost_*`, расходы, `exchange_rate`)
**скрываются** в шаблонах от ролей без `can_view_purchase_cost`.

## 11a. Навигация

Пункт **«Партии»** виден ролям, которым он нужен:
`can_manage_batches OR can_view_purchase_cost OR is_storekeeper`
(Админ, Руководитель, Наблюдатель, Кладовщик). Продавцу/Мастеру не показываем.

## 12. Обязательные тесты

1. Создание партии.
2. Генерация номера партии (формат `П-…`, без дублей при двух подряд).
3. Создание строки партии.
4. Сумма строки считается через `Decimal` (точные копейки; не float).
5. `BatchLine` **не создаёт остаток** (нет `StockLot`/`PartItem`/`StockBalance` —
   этих моделей ещё нет; проверяем, что строка — обычная запись, и приложений
   остатков нет).
6. `cost_finalized` по умолчанию `false`.
7. Партия с `cost_finalized=false` помечается недоступной к будущей продаже
   (`is_available_for_sale is False`).
8. Строку можно удалить **только в `DRAFT`**.
9. После перехода из `DRAFT` удаление строки запрещено (403/блокировка).
10. Права: не-`MANAGE_BATCHES` не может создавать/редактировать партии → 403.
11. Закупочные суммы **скрыты** от роли без `can_view_purchase_cost` (кладовщик
    видит карточку, но не видит цен).
12. Навигация показывает «Партии» нужным ролям (кладовщик — да; продавец — нет).

## 13. Ручная проверка

1. Войти администратором → создать партию (поставщик, валюта, курс, расходы) →
   добавить строки (деталь, количество, цена) → увидеть суммы по строкам и итог.
2. Перевести партию `draft → ordered → … → accepted`.
3. В `draft` удалить строку — можно; после `ordered` — нельзя.
4. Войти кладовщиком → партии видны, но без закупочных цен; редактировать нельзя.
5. Войти продавцом → раздела «Партии» нет.
6. Убедиться, что нигде не появился остаток на складе.

## 14. Критерии готовности

1. `Batch` и `BatchLine` создаются/редактируются; номер партии без дублей.
2. Суммы строк считаются через `Decimal` до копеек.
3. `cost_finalized=false`; landed cost не считается; остатки не создаются.
4. Удаление строк — только в `DRAFT`; статусные переходы — в разрешённых пределах.
5. Доступ: управление — `MANAGE_BATCHES`; суммы — `can_view_purchase_cost`.
6. «Партии» в навигации нужным ролям.
7. Все обязательные тесты зелёные; `ruff`/`djlint` чисты; миграции с нуля;
   `makemigrations --check` чисто.
8. `StockLot`/`PartItem`/`StockMovement`/`StockBalance`/сканер/размещение/продажи
   **не реализованы**.

## 15. Файлы (создаются/изменяются)

**Создаются — `procurement`:**
- `apps/procurement/{__init__,apps,models,admin,forms,views,urls}.py`
- `apps/procurement/migrations/0001_initial.py`
- `templates/procurement/batch_list.html`
- `templates/procurement/batch_detail.html`
- `templates/procurement/batch_form.html` (или переиспользование `directories/form.html`)
- `templates/procurement/line_form.html`
- `tests/test_batches.py`

**Изменяются — accounts/config:**
- `apps/accounts/roles.py` — `MANAGE_BATCHES`
- `apps/accounts/models.py` — `can_manage_batches`
- `apps/accounts/permissions.py` — `ManageBatchesMixin`
- `apps/accounts/context_processors.py` — пункт «Партии»
- `config/settings/base.py` — `LOCAL_APPS += procurement`
- `config/urls.py` — подключение маршрутов `procurement`

## 16. Что будет закоммичено

Один коммит слоя:

```
Слой 6: партии и строки поступления (Batch, BatchLine)
```

После `pytest`, `ruff`, `djlint`, `makemigrations --check` — коммит и остановка
перед Слоем 7 (landed cost и фиксация себестоимости).

## Границы слоя (чего НЕ делаем)

- Не реализуем landed cost и не распределяем доставку/таможню/комиссии по деталям.
- Не делаем `cost_finalized=true`.
- Не создаём `StockLot`, `PartItem`, `StockMovement`, `StockBalance`.
- Не реализуем поступление через сканер и размещение по ячейкам.
- Не реализуем продажи, резервы, установки, возвраты и аналитику.
- `BatchLine` остаётся **документальной строкой партии**, а не остатком.
