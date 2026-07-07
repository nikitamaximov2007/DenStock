# Current handoff

Нет активной передачи: последняя задача завершена.

- Task: Hotfix Layer 32.3.1 - BRP exact material number outranks replacement
  match (DONE)
- Branch: main
- Completed: полностью. find_brp_by_number: точное совпадение material_no
  всегда выше совпадения по замене (внутри группы ненулевая розница, затем
  pk); refresh_draft_prices переразрешает номер строки и перепривязывает
  строки черновиков к правильной позиции BRP (название/цена обновляются,
  количество/сканы нет); проведённые сессии не трогаются. 6 новых тестов,
  документация (руководство + операционная заметка BRP).
- Unfinished: nothing.
- Tests run: full pytest (789 passed), ruff, djlint, manage.py check,
  makemigrations --check.
- Known risks: none; продакшен-кейс (сессия 3, строка 23: 417224916,
  привязана к 417224458 с ценой 0) чинится открытием страницы сессии после
  деплоя, пересканирование не нужно.
- Next exact steps for Codex: none, no handoff active.
- Do not touch: applied migrations, posted documents, stock posting flows.
