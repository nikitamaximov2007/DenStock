# Current handoff

Задача завершена.

- Task: пакетная приёмка exact-артикулов по ячейкам и улучшение журнала движений.
- Branch: `feature/batch-scanner-receiving`.
- Base: `origin/main` at `22bfe8c` (Merge unified price settings).
- Реализовано:
  - журнал движений показывает exact-артикул вместо номера лота;
  - единый фильтр целых значений на Decimal ROUND_HALF_UP;
  - session-очередь, группировка по ячейкам, исправление/удаление/очистка;
  - exact lookup по складу, BRP и Polaris без replacement/superseded identity;
  - первый лот нового каталожного вида без Receipt;
  - атомарное проведение через inventory service и постоянный idempotency token;
  - старый ITEM:/DS/серийный поток сохранён;
  - документация и целевые тесты обновлены.
- Миграция: `inventory/0010_found_stock_posting.py`, только новая таблица
  использованных токенов; исторические лоты/движения не меняет.
- Проверено: 911 тестов зелёные; ruff, djlint, Django check,
  makemigrations --check и git diff --check чистые. Queue GET = 9 SQL-запросов.
  Playwright smoke прошёл на desktop и 375 px: autofocus, exact identity,
  группировка, новая деталь, смена ячейки и отсутствие page overflow.
- Commit/push: выполняются после этого обновления; hash будет в финальном отчёте.
- Риски для финальной проверки: служебная партия «Стартовый ввод» создаётся
  только если для part/location нет доступного лота; в `/receipts/` она не
  появляется. Оценка первого лота берётся из существующего recommended_price.
