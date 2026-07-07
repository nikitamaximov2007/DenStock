# Current handoff

Нет активной передачи: последняя задача завершена.

- Task: Hotfix Layer 32.3 - cell value totals, quantity totals, cleaner
  summary, BRP duplicate price preference (DONE)
- Branch: main
- Completed: полностью. Итоги «Всего деталей в ячейке» и «Стоимость ячейки»
  (counters + агрегаты списка), колонка «Деталей»/«Стоимость» в списке,
  плитка «Найдено в складе» убрана из сводки (источник строк остался),
  импорт BRP выбирает лучшую строку дубликата (розница>0 -> оптовая>0 ->
  первая), реимпорт чинит нулевые цены (счётчики duplicates_price_resolved,
  zero_price_repaired), find_brp_by_number предпочитает ненулевую розницу,
  refresh_draft_prices освежает снимки цен ТОЛЬКО черновиков при открытии
  страницы сессии/обзора (статус проверяется из базы), 24 новых теста, доки.
- Unfinished: nothing.
- Tests run: full pytest (783 passed), ruff, djlint, manage.py check,
  makemigrations --check; браузерный smoke (детали/список/375px).
- Known risks: none; проведённые сессии и документы не переписываются
  (тест test_posted_session_prices_not_refreshed).
- Next exact steps for Codex: none, no handoff active. Для продакшена нужен
  реимпорт прайса (см. docs/operations/brp-catalog-import.md, раздел
  «Реимпорт чинит нулевые цены»).
- Do not touch: applied migrations, posted documents, stock posting flows.
