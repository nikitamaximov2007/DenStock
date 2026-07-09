# Current handoff

Нет активной передачи: последняя задача завершена.

- Task: Layer 34 - move counting inventory into stocktaking (DONE)
- Branch: main
- Completed: полностью.
  - InventoryCountingSession.inventory_number (IC-xxxxxx из общего счётчика
    inventory_count - единая нумерация с документами сверки, коллизий нет);
    миграция counting/0002; post_session присваивает номер.
  - counting_post редиректит на /stocktaking/initial/<pk>/ с сообщением
    «Пересчёт завершён. Создан документ инвентаризации IC-...».
  - /stocktaking/: блок «Первичный ввод ячеек (из пересчёта)» (номер,
    ячейка, описание, позиций, итоговое количество, сумма оценки) + detail
    страница initial_inventory_detail (строки из пересчёта автоматически:
    номер/название/источник/ячейка/количество/оценка за ед./сумма/статус,
    итог = стоимости ячейки, техдокумент упомянут без ссылки).
  - /receipts/: список фильтрует counting_session__isnull=True - POS-3..6
    исчезнут после деплоя автоматически; физически целы (лоты/движения/
    партии ссылаются). Detail технически доступна по прямому URL.
  - Страницы пересчёта не ссылаются на поступления; плитки «Событий
    сканирования» / «Итоговое количество» + подсказка про ручные правки.
  - Команда migrate_counting_receipts_to_stocktaking (--dry-run/--commit):
    присваивает IC-номера историческим POSTED-сессиям, идемпотентна,
    склад не трогает, отчёт found/assigned/skipped/hidden.
  - Команда delete_draft_receipts --receipt-id N (--commit): только
    черновики, отказ для проведённых и связанных с пересчётом (для решения
    судьбы POS-000001/000002 пользователем, не молча).
  - Физика склада НЕ менялась: остатки создаются прежним post_receipt
    внутреннего документа; новых движений нет; двойное проведение сессии
    по-прежнему заблокировано.
- Tests run: full pytest (845 passed; 7 новых), ruff, djlint, manage.py
  check, makemigrations --check. Браузерный smoke: полный поток convert ->
  post -> IC-000001 в /stocktaking/, /receipts/ без POS, остатки не
  удвоены (15 = 2 старых + 13), 375px без переполнения. backup_all/
  ops_check локально не работают (нужен pg_dump) - это продовые команды.
- Next exact steps for Codex: на проде после деплоя выполнить
  migrate_counting_receipts_to_stocktaking по инструкции в
  docs/operations/brp-catalog-import.md.
- Do not touch: applied migrations, stock posting flows, движения/остатки.
