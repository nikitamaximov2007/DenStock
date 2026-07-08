# Current handoff

Нет активной передачи: последняя задача завершена.

- Task: Hotfix Layer 33.1 - counting-created receipts use per-line cell
  customer value (DONE)
- Branch: main
- Completed: полностью.
  - convert_to_receipt: каждая строка документа получает
    final_customer_price_rub своей строки пересчёта; глобальный unit_cost -
    только запасная цена для строк без цены (по умолчанию 0, строка без
    цены остаётся 0). Итог документа = «Стоимости ячейки».
  - convert.html: карточка «Документ первичного ввода», «Цены будут взяты
    из пересчёта ячейки», «Итоговая стоимость документа: X ₽», поле
    переименовано в «Запасная цена только для строк без цены (₽)».
  - receipt_detail: для документов из пересчёта (receipt.counting_session
    .exists()) метки «Оценка за ед. (₽)» / «Сумма оценки» + пояснение;
    обычные поступления от поставщика не изменились.
  - Команда repair_counting_receipt_prices (--session-id|--receipt-id,
    dry-run по умолчанию / --commit): переносит цены строк пересчёта в уже
    созданный документ; чинит ТОЛЬКО документы, связанные с пересчётом;
    меняет только unit_cost_rub строк - количества, остатки, лоты, движения
    не трогаются; transaction.atomic. Продакшен: --session-id 3 (или
    --receipt-id 3) -> POS-000003 станет 460305 ₽.
- Caveat (зафиксировано ранее в 33.1-обсуждении): landed cost лотов,
  созданных проведением POS-000003 с нулями, командой НЕ переписывается
  (история движений/партий не мутируется) - чинится только оценка
  документа. Если Денису нужна переоценка лотов для статистики стоимости
  склада, это отдельное решение/слой.
- Tests run: full pytest (838 passed; 8 новых), ruff, djlint, manage.py
  check, makemigrations --check (миграций нет).
- Next exact steps for Codex: none, no handoff active.
- Do not touch: applied migrations, stock posting flows, движения.
