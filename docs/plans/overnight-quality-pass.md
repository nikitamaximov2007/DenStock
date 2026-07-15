# Overnight quality pass

Рабочий аудит перед реализацией. Ветка `feature/overnight-quality-pass`, база
`508a2bc47b66afd1adb075eb01beb8da0f102c0e`. Production в задаче не используется.

## 1. Поиск и сканирование до унификации

Точки поиска складской детали:

- `/search/`: `apps.core.views.search_page` -> `apps.core.search.search_parts`.
  Ищет `PartType.name`, все `PartNumber`, `PartBarcode`, внутренние и серийные
  номера `PartItem`, а также совместимость. Количество берёт сначала из
  `StockBalance`, затем из первичных таблиц только при отсутствии cache-строк.
- `/scanner/` и `/scanner/resolve/`: `apps.core.scanner.resolve_scan`.
  Разрешает системные barcode экземпляра и ячейки, внутренний номер, партию,
  код ячейки, заводской barcode, серийный номер и `PartNumber`.
- `/scanner/receiving/`: тот же `resolve_scan`, затем очередь приёмки. Групповое
  проведение использует `FoundStockPosting.token` и `post_found_stock_group`.
- `/scanner/move/`: `resolve_scan`, затем live selectors из
  `apps.inventory.movement`; проведение использует `StockTransfer.token`.
- `/inventory/actions/`: собственный `apps.actions.services.resolve_part`,
  затем `stock_overview` по available `StockLot` и активным резервам.
- `/counting/<id>/scan/`: `apps.counting.services._match` и
  `_warehouse_part_by_scan`; затем отдельные BRP/Polaris resolvers. Скан меняет
  только draft counting, склад меняется позже через `receipts.post_receipt`.
- `counting_line_resolve`: прямые запросы `PartNumber`/`PartBarcode`, затем
  отдельный BRP/Polaris lookup.
- `/brp/` и `/polaris/`: каждая страница отдельно ищет warehouse card по
  `PartNumber`, `PartBarcode` и названию, затем свой reference catalog.
- Формы продаж, ремонта, возвратов, списаний и ручных корректировок выбирают
  конкретные `PartItem`/`StockLot` QuerySet. Они не используют текстовый
  resolver, но их доступность должна совпадать с live lookup.

Нормализация номера находится в `apps.catalog.models.normalize_number` и
удаляет пробелы и разделители. Exact identity определяется в
`apps.inventory.presentation`: OEM/ARTICLE являются identity; ANALOG,
replacement, superseded и price source identity не являются. BRP replacement
и Polaris superseded попадают в warehouse `PartNumber` как ANALOG.

Основные расхождения до этапа A:

- глобальный поиск считает количество через `StockBalance`, actions только по
  available bulk lots, movement уже использует живые `StockLot`/`PartItem`;
- actions и counting молча возвращают not found при неоднозначном номере;
- catalog pages, scanner и общий поиск имеют разные приоритеты;
- `resolve_scan` не возвращает категорию, manufacturer и live quantities;
- аппаратный Enter защищён focus-скриптом только частично, actions и counting
  не имеют durable idempotency token.

## 2. Live quantity

Canonical read selector находится в `apps.inventory.movement.live_stock_rows`.
Источник физики: `StockLot` со статусами из `LOT_PHYSICAL_STATUSES` и
`PartItem` из `ITEM_PHYSICAL_STATUSES`. Активные резервы читаются из sales
services, quarantine показывается отдельно. `InventoryCountingLine` является
историей, `StockBalance` является rebuildable cache и не должен становиться
независимой истиной.

## 3. Ремонт и возврат

- Документ: `RepairOrder`, строки: `RepairIssueLine`.
- Статусы: `draft`, `completed`, `canceled`.
- Выдача: `complete_repair_order` под transaction/select_for_update вызывает
  `inventory.issue_part_item` или `inventory.issue_stock_lot`.
- Возврат: `StockReturn` и `StockReturnLine`, источник явно связан с
  `RepairIssueLine`. `complete_return` блокирует документ и строки, повторно
  проверяет лимит и вызывает inventory return services.
- Завершённый возврат создаёт `WarehouseAction(REPAIR_RETURN)` со snapshot
  exact number, manufacturer, repair issue line и stock return.
- `calculate_repair_costs` уже вычитает завершённые возвраты из стоимости.
- Отмена проведённого возврата отсутствует. `StockReturn` имеет только draft и
  completed. Отмена проведённого ремонта запрещена, canceled доступен только
  для draft.

## 4. Таможенный Excel до исправления

`actions_export` получает active `WarehouseAction` по фильтрам и полностью
исключает `REPAIR_RETURN`. `build_export_rows` суммирует положительное quantity
по ключу `manufacturer snapshot + exact part_number snapshot`. Поэтому возврат
из ремонта не уменьшает экспортируемый расход. Polaris B/E/F и общий формат
заполняются `part_export_data` и `export_customs_xlsx`.

Canonical требование этапа B: repair quantity является signed ledger внутри
export selector: active REPAIR добавляет, active REPAIR_RETURN вычитает,
отменённые действия не участвуют, результат группировки ограничен нулём.

## 5. Настройки цен

- Единый курс: `warehouse.ValuationSettings.current_usd_rate`, Decimal(10, 4).
- Наценки: `BrpPricingSettings.brp_markup_percent` и
  `PolarisPricingSettings.polaris_markup_percent`, Decimal(6, 2).
- Форма `catalog.forms.PriceSettingsForm` уже использует
  `CommaDecimalField`, server-side замену запятой, Decimal, min values и HTML
  step `0.0001`/`0.01`.
- POST идёт через `catalog.services.update_current_price_settings`; linked
  calculated prices обновляются существующим pricing service, manual prices
  не переписываются.
- Browser preview в `templates/directories/price_settings.html` использует
  JavaScript `Number`, поэтому этап C должен убрать float из preview и добавить
  regression tests повторного открытия формы и дробной valuation.
- Цены/себестоимость в документах имеют DecimalField(2); складские quantities
  DecimalField(3); sequence/capacity/year поля остаются целыми по смыслу.

## 6. Layout и navigation

`templates/base.html` имеет стабильные `#app-sidebar` и `#content`, messages
находятся внутри content. `static/js/app_shell.js` сохраняет только open state
групп в localStorage и закрывает mobile menu по Escape. HTMX и Turbo нет.

Глобальные scripts:

- `scanner.js`: topbar scanner, JSON POST и result rendering;
- `scan_focus.js`: autofocus и защита от слишком близких Enter;
- `image_gallery.js`: gallery/modal handlers;
- `app_shell.js`: layout behavior.

Inline scripts присутствуют как минимум в настройках цен, customs quick edit и
некоторых scanner/templates. После partial replacement они не исполнятся сами,
поэтому navigation controller должен публиковать единое событие и выполнять
явно разрешённые page initializers. Download/Excel, logout, POST, upload,
external, target и явно full-load ссылки перехватывать нельзя.

Выбранный подход этапа D: небольшой локальный fetch controller без CDN и без
frontend framework, с обычным Django GET как fallback. Sidebar scrollTop
дополнительно сохраняется в sessionStorage и восстанавливается при полной
загрузке.

## 7. Проверки по этапам

- A: canonical lookup, scanner ambiguity/live quantities, idempotency и N+1.
- B: signed net repair consumption, return limits, status transitions и Excel.
- C: comma/dot Decimal settings, persistence и valuation equality.
- D: template contracts, fallback exclusions, reinitialization contract и
  manual browser checklist.

Документ обновляется по мере реализации и будет содержать финальные команды и
известные ограничения.

## 8. Результат этапа A

- Добавлен `apps.core.part_lookup.resolve_part_lookup`: единая нормализация,
  приоритет exact/barcode/alias/name, явная неоднозначность и exact identity.
- Live quantities читаются из текущих `StockLot`/`PartItem`; cache
  `StockBalance` используется только как метка совместимости старого UI.
- Единый resolver подключён к общему сканеру и поиску, списку деталей,
  BRP/Polaris, пакетной приёмке, actions и пересчёту ячейки.
- Actions и counting scans получили durable request token; повторный POST не
  создаёт вторую операцию. Складские мутации остаются в существующих services.
- Добавлены миграции actions `0008` и counting `0004`, обе nullable/additive.
- Targeted и warehouse regression suite: 432 passed. Отдельные проверки:
  ruff, djlint, Django check, migration check и `git diff --check`.
