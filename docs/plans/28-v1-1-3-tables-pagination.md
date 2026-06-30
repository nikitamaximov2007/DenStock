# План v1.1.3 — Таблицы и пагинация (P0/P1 из аудита)

**Статус:** ПЛАН (реализацию не начинаем без отдельного «go»). Закрывает **P0-2** (нет
пагинации → записи за 50-й недостижимы) и часть **P1** (числа/empty-state) из
[docs/audits/01-v1-ui-ux-audit.md](docs/audits/01-v1-ui-ux-audit.md).

**Суть:** не «красим всё», а чиним конкретную UX-дыру — списки не должны терять данные после
50-й записи и должны нормально читаться. Только шаблоны; backend/логику/queryset не трогаем.

---

## 1. Анализ (факты)

- **Пагинация в шаблонах отсутствует полностью** (grep `page_obj`/`is_paginated`/`pagination` по
  `templates/` — 0). При этом 6 view имеют `paginate_by = 50` → 51-я запись и далее
  **недостижимы** пользователю.
- **`paginate_by = 50` есть ровно у 6 class-based ListView:**
  | View | Шаблон | Контекст | Числовые колонки |
  |---|---|---|---|
  | `PartTypeListView` | `catalog/part_list.html` | `object_list`/`page_obj` | Реком. цена |
  | `PartItemListView` | `inventory/item_list.html` | `items` | Себестоимость (под `show_costs`) |
  | `StockLotListView` | `inventory/lot_list.html` | `lots` | Количество; Себестоимость |
  | `MovementListView` | `inventory/movement_list.html` | `movements` | Кол-во; Сумма |
  | `BalanceListView` | `inventory/balance_list.html` | `balances` | Физически/Доступно/Карантин |
  | `BatchListView` | `procurement/batch_list.html` | `batches` | Доп. расходы |
- **Списки sales/reservations/repairs/returns/writeoffs/stocktaking — функциональные view БЕЗ
  пагинации** (рендерят все строки). Добавление пагинации туда = изменение view-логики
  (Paginator), это **вне** правила «там, где `paginate_by` уже есть» → выносим в следующий
  подэтап (см. §8).
- Все 6 списков имеют `{% empty %}`-строку `<td colspan=N class="muted">…нет.</td>` (плоско,
  не `.empty`); числа — сырьём, влево.
- **Django 5.2.15** → доступен встроенный тег **`{% querystring %}`** (с 5.1): сохраняет
  текущие GET-фильтры при смене страницы **без кастомного Python**.

---

## 2. Scope этапа — 6 списков + 1 партиал (7 файлов, ≤8)

Берём ровно те списки, где `paginate_by` **уже есть** (риск минимальный, view не трогаем):

**Создаётся:**
- `templates/partials/_pagination.html` — переиспользуемый пагинатор.

**Изменяются (только разметка):**
- `templates/catalog/part_list.html`
- `templates/inventory/item_list.html`
- `templates/inventory/lot_list.html`
- `templates/inventory/movement_list.html`
- `templates/inventory/balance_list.html`
- `templates/procurement/batch_list.html`

> Это покрывает приоритетные экраны заказчика (PartType/PartItem/StockLot/Batch) + два
> инвентарных списка, которые тоже уже пагинированы (Movement/Balance). Sales/Reservations —
> следующий подэтап (§8), т.к. требуют добавления пагинации в функциональные view.

---

## 3. Пагинатор (`partials/_pagination.html`)

Читает `page_obj`/`is_paginated` (их даёт ListView автоматически). Сохраняет фильтры через
встроенный `{% querystring %}`. **Переиспользует существующие классы** (`.toolbar`,
`.btn--small`, `.btn--secondary`, `.muted`) — **нового CSS не требуется**.

```django
{% if is_paginated %}
  <nav class="toolbar" aria-label="Постраничная навигация">
    {% if page_obj.has_previous %}
      <a class="btn btn--small btn--secondary"
         href="{% querystring page=page_obj.previous_page_number %}">← Назад</a>
    {% endif %}
    <span class="muted">Страница {{ page_obj.number }} из {{ page_obj.paginator.num_pages }}
      ({{ page_obj.paginator.count }} всего)</span>
    {% if page_obj.has_next %}
      <a class="btn btn--small btn--secondary"
         href="{% querystring page=page_obj.next_page_number %}">Вперёд →</a>
    {% endif %}
  </nav>
{% endif %}
```

Подключение в каждый список — `{% include "partials/_pagination.html" %}` сразу после таблицы.

---

## 4. Приведение таблиц subset к базовому виду

В каждом из 6 шаблонов (точечно, презентационно):

1. **Числовые колонки** — класс на `<th>` и `<td>`:
   - количества → `.num--qty` (lot.quantity, movement.quantity, balance.quantity_*);
   - деньги → `.num--money` (item/lot/movement себестоимость/сумма, batch доп. расходы, part реком. цена).
2. **Empty-state** — заменить `{% empty %}`-строку на блок `.empty` через `{% if <rows> %} …таблица…
   {% include _pagination %} {% else %} <div class="empty">…</div> {% endif %}`:
   - part: «Деталей не найдено» / «Деталей пока нет»;
   - item: «Экземпляров нет»; lot: «Лотов нет»; movement: «Движений нет»;
   - balance: «Остатков нет»; batch: «Партий нет».
   (С короткой подсказкой `empty__text`, без CTA — чтобы не плодить логику прав в этом этапе.)
3. **`.table__actions`** — в этом subset списках нет колонок с кнопками-действиями (только
   ссылки-переходы), поэтому класс пока не нужен; оставляем на этап с CRUD-списками.
4. **Статусы** — массовый pill-проход **не делаем** (отдельный этап). Здесь не трогаем
   `get_status_display` (минимизируем диффы).

> `colspan` в старых `{% empty %}` уйдёт вместе с переходом на `.empty`-блок (заодно
> устраняется хардкод-`colspan` при условных колонках `show_costs`).

---

## 5. UI-компоненты (всё из v1.1.1, нового CSS нет)

`.num` / `.num--qty` / `.num--money`, `.empty` (+ `__title`/`__text`), `.btn--small`,
`.btn--secondary`, `.toolbar`, `.muted`. CSS-файл **не меняем**.

---

## 6. Риски

- **Низкие.** Только разметка 6 списков + новый партиал. View/queryset/фильтры не трогаем —
  `page_obj` и фильтры уже в контексте; `{% querystring %}` сохраняет GET-параметры.
- Мелкий риск — рассинхрон `{% if rows %}` с именем переменной контекста (у каждого списка своё:
  `items`/`lots`/`movements`/`balances`/`batches`/`object_list`). Закрывается аккуратной правкой
  по факту + тестами открытия каждого списка.

---

## 7. Тесты (`tests/test_pagination.py`)

1. Партиал **не рендерится**, когда страниц одна (`is_paginated=False`) — нет «Вперёд».
2. На `part_list` с **>50** PartType (дёшево создать, без склада) видны controls и ссылка
   `?page=2`; `GET ?page=2` → 200 и есть «← Назад».
3. Фильтр сохраняется при переходе по странице (`?show=all&page=2` содержит `show=all`).
4. **Empty-state**: пустой `item_list`/`lot_list`/… показывает блок `.empty` («…нет»), а не
   только заголовки таблицы.
5. Все 6 списков открываются (200) для пользователя с правом.
6. GET любого из списков **не создаёт** `StockMovement`.
7. GET любого из списков **не меняет** `StockBalance`.
8. `pytest` / `ruff` / `djlint --check` / `makemigrations --check` (изменений нет) /
   `manage.py check` — зелёные.

---

## 8. Что НЕ трогаем (границы)

- Не меняем: модели, миграции, services, permissions, scanner/barcodes, `StockMovement`,
  `StockBalance`, бизнес-логику, queryset/фильтры view.
- Не добавляем JS и зависимости; CSS-файл не меняем (классы уже есть в v1.1.1).
- Не трогаем detail-страницы и формы (отдельные этапы аудита).
- **Не делаем** массовый status-pill проход (отдельный этап v1.1.4).
- **Следующий подэтап (не сейчас):** пагинация для функциональных списков
  sales/reservations/repairs/returns/writeoffs/stocktaking — потребует добавить `Paginator`
  в их view (минимальная правка view + шаблон), вынесем в `v1.1.3b`.

---

## 9. Файлы и коммит

**Файлы (7):** `templates/partials/_pagination.html` (new) + 6 списков
(`catalog/part_list`, `inventory/item_list`, `inventory/lot_list`, `inventory/movement_list`,
`inventory/balance_list`, `procurement/batch_list`) + тест `tests/test_pagination.py`.
Итого 8 с тестом — на границе; Python-код приложений **не меняется**.

**Коммит реализации (после «go» и зелёных проверок):** `UI: таблицы и пагинация`.
