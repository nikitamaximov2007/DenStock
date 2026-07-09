# Current handoff

Нет активной передачи: задача завершена.

- Task: Приёмка сканером — довнесение найденной детали (+1) по обычному
  номеру детали (DONE)
- Branch: main
- Verified merged Codex work first: identity fix + cancellation + repair
  command + Polaris catalog — все на main, 869 passed, ruff/djlint/check/
  makemigrations чисто.
- Реализовано:
  - inventory.services.add_found_stock(part, location, qty=1, by, comment):
    +qty к существующему доступному лоту (part,location) через
    adjust_stock_lot_quantity (ADJUST_IN, document_type="found_addition");
    если лота в ячейке нет — InventoryError (молча не кладём); atomic,
    select_for_update, защита от минуса. FOUND_ADDITION_DOC константа.
  - core.views.scanner_receiving: скан обычного номера (result.type ==
    "part_type") больше НЕ ошибка «это вид детали», а сценарий «довнести
    найденную деталь»: _prepare_found_addition (ищет ячейки через
    actions.stock_overview, ставит одноразовый token в сессию),
    _handle_found_addition (потребляет token ДО мутации — защита от двойного
    сабмита, зовёт add_found_stock, сообщение с новым остатком, PRG).
    exact номер = actions.identity_number (не замена). found_history
    (ADJUST_IN + found_addition) в ctx.
  - templates/core/receiving.html: карточка добавления (номер/название/оценка/
    ячейки с выбором), кнопка «Добавить +1 к наличию», таблица «Добавление
    найденных деталей». Экземпляры ITEM:/DS-… — старый флоу без изменений.
  - Цена = customer/evaluation (part.recommended_price), не себестоимость.
  - Миграций НЕТ (переиспользованы существующие таблицы/движения).
- Не тронуты: sale/reserve/repair, customs export, receipts, stocktaking,
  BRP exact/replacement логика.
- Checks: full pytest 881 passed (12 новых в test_scanner_stock_addition.py),
  ruff clean, djlint clean, manage.py check, makemigrations --check без
  изменений. Браузерный smoke: скан 420931285 -> карточка (не 420931284) ->
  +1 (3->4) -> /actions «Доступно 4», /receipts чист, 375px без переполнения.
