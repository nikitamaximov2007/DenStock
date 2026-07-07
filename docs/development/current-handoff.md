# Current handoff

Нет активной передачи: последняя задача завершена.

- Task: Layer 32.4 - cell value breakdown modal + safe promotion effective
  prices (DONE)
- Branch: main
- Completed: полностью.
  - get_session_value_breakdown(session): единый разбор стоимости для
    модалки и тестов (rows: number/name/source_label/quantity/
    customer_price_rub/line_total_rub + total_quantity/total_value_rub/
    positions_count); порядок строк = таблице пересчёта; нулевые строки
    видны и дают 0.
  - Плитка «Стоимость ячейки» кликабельна (href="#value-breakdown",
    подпись «Нажмите для расчёта»); модалка на чистом CSS :target (без JS,
    как весь модуль пересчёта): заголовок «Расчёт стоимости ячейки»,
    колонки Номер/Название/Источник/Кол-во/Цена клиента/Расчёт/Сумма,
    блок «Сумма строк -> Итого + формула» (value-total, визуальная скобка
    border-left), закрытие ссылкой на #scan (кнопка «Закрыть» и бэкдроп).
  - convert_to_receipt: перед конвертацией вызывает refresh_draft_prices
    (правильная привязка 32.3.1 + эффективная цена 32.3.2); при промоушене
    BRP-строки с нулевой розницей личности эффективная цена передаётся как
    manual_price (снимок честный: calculated=0, manual=616, final=616);
    ненулевые личности идут обычным CALCULATED-путём.
- Замечание/caveat: прямое «Создать карточку» из BRP-поиска (brp_promote)
  по-прежнему снимает цену только с самой позиции: правило эффективной
  цены действует в конвертации пересчёта. Если нужно и там - отдельный слой.
- Tests run: full pytest (802 passed; 7 новых), ruff, djlint, manage.py
  check, makemigrations --check (миграций нет). Браузерный smoke: модалка
  скрыта по умолчанию, открывается с плитки, итог равен плитке, закрытие
  возвращает к #scan, 375px без переполнения (таблица скроллится внутри).
- Known risks: prod отдаёт app.css через WhiteNoise без хэшей в имени -
  после деплоя браузеры могут до минуты держать старый CSS (max-age 60);
  модалка в это время просто отрисуется как блок внизу страницы, не ломая
  ничего.
- Next exact steps for Codex: none, no handoff active.
- Do not touch: applied migrations, posted documents, stock posting flows.
