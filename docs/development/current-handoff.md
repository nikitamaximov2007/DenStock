# Current handoff

Нет активной передачи: последняя задача завершена.

- Task: Layer 33 - scanner warehouse actions, unified report, customs Excel
  export (DONE). Разделы 0-1 спецификации (эффективные цены при промоушене и
  кликабельный разбор стоимости ячейки) были реализованы ранее слоями
  32.4/32.4.1 и покрыты их тестами.
- Branch: main
- Completed: полностью.
  - Новое приложение apps.actions: WarehouseAction (журнал для отчёта,
    ссылки на созданные документы) + PartCustomsInfo (OneToOne к PartType:
    RU-название manual/auto, веса брутто/нетто ТОЛЬКО ручные, источник/
    проверено, страна КАНАДА, область МОТО ЗАПЧАСТИ). Миграция
    actions/0001_initial (новые таблицы, существующие не тронуты).
  - Физику склада НЕ дублирует: продажа = create_sale + add_stock_lot_to_
    sale + complete_sale; резерв = create/add/activate_reservation; ремонт =
    create/add/complete_repair_order. Раскладка количества по лотам ячейки
    FIFO, всё в одной транзакции, отрицательный остаток невозможен.
  - Экраны: /inventory/actions/ (скан -> ячейки -> действие), /report/
    (фильтры, итоги, готовность к экспорту), /export/ (xlsx по шаблону
    docs/templates/supplier_order_template.xlsx: лист «Лист1», строки 1-9
    целы, данные с 10-й, пример 271002228 очищается, формулы I/L, верхний
    регистр, K = розница BRP USD от эффективного источника, пустые веса
    остаются пустыми), /customs/<part_id>/ (ручные таможенные данные).
  - Автопоиск весов в интернете НЕ реализован (отложен как Layer 34 по
    спецификации): только ручные/проверенные веса.
  - .gitignore: исключение !docs/templates/supplier_order_template.xlsx.
- Caveats: быстрые действия работают с количественными лотами; поштучные
  (серийные) детали направляются в существующий флоу карточки детали.
  Кнопка «Создать карточку» BRP-поиска по-прежнему вне правила эффективной
  цены (как решено в 32.4).
- Tests run: full pytest (830 passed; 22 новых в test_actions + 2 smoke),
  ruff, djlint, manage.py check, makemigrations --check. Браузерный smoke:
  скан/мультиячейки/продажа/отчёт/таможенная форма, 375px без переполнения.
- Next exact steps for Codex: none, no handoff active.
- Do not touch: applied migrations, posted documents, stock posting flows.
