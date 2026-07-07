# Current handoff

Нет активной передачи: последняя задача завершена.

- Task: Hotfix Layer 32.3.2 - use priced BRP replacement as price source
  when exact part has zero price (DONE)
- Branch: main
- Completed: полностью. find_brp_price_source разделяет личность строки и
  источник цены: точный номер остаётся привязкой, при нулевой рознице цена
  берётся из связанной по цепочке замен позиции с розницей > 0 (обратные
  ссылки на номер + замены самой позиции; порядок розница > 0 -> pk).
  Подключено в скан (_match), refresh_draft_prices и разбор неизвестных
  строк. 6 новых тестов, документация.
- Unfinished: nothing. Замечание на будущее (НЕ реализовано, вне рамок):
  promote_to_warehouse при конвертации по-прежнему снимает цену с самой
  позиции BRP; для позиций с розницей 0 карточка получит цену 0, хотя
  пересчёт показывал цену от замены. Если нужно, вынести отдельным слоем.
- Tests run: full pytest (795 passed), ruff, djlint, manage.py check,
  makemigrations --check.
- Known risks: none; продакшен-кейс (сессия 3, строка 38: 250000059 с ценой
  0 при наличии 250000418 с 4.19 $) чинится открытием страницы сессии после
  деплоя; проведённые сессии не переписываются.
- Next exact steps for Codex: none, no handoff active.
- Do not touch: applied migrations, posted documents, stock posting flows.
