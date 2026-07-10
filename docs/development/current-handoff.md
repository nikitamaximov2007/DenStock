# Current handoff

Нет активной передачи: задача завершена.

- Task: Финансовая статистика склада — три показателя на /statistics/ (DONE)
- Branch: feature/warehouse-financial-statistics (НЕ мержить без решения
  пользователя; в main не пушилось)
- Как было: «Стоимость склада» считалась по landed cost (после
  инвентаризаций первичного ввода он нулевой — карточка бесполезна);
  «Потенциальная выручка» = available x recommended_price.
- Как стало (apps/reports/warehouse_finance.py, read-only):
  1. «Закупочная стоимость склада» = физический остаток x базовая цена USD
     x курс настройки (default 105). BRP: retail_price_usd (база формулы
     клиентской цены, ДО наценки) от effective source (replacement при
     нуле); Polaris: ТОЛЬКО wholesale («ОПТОВАЯ»), при нуле — оптовая от
     superseded-связи (identity не подменяется).
  2. «Оценка склада по цене продажи» = физический остаток x действующая
     клиентская цена (существующие формулы pricing BRP/Polaris, настройки
     цен читаются один раз на расчёт — нет N+1). Ручные карточки —
     recommended_price.
  3. «Потенциальная прибыль» = разница. Без учёта доставки/таможни/налогов
     (пояснение в UI); фактическая себестоимость — отдельная будущая задача.
  - Позиции без закупочной цены: исключаются из закупки, счётчик
    «Без закупочной цены: N позиций / N единиц» + tooltip.
  - Физический остаток: LOT_PHYSICAL_STATUSES + ITEM_PHYSICAL_STATUSES
    (та же физика, что в «Остатках»); резервы не вычитаются (по ТЗ).
- Курс: apps/warehouse/models.ValuationSettings (singleton,
  purchase_usd_rate=105, «Курс для оценки закупочной стоимости»),
  миграция warehouse/0002. В шаблоне курс не хардкодится.
- UI (templates/reports/statistics.html): три плитки с подписями, строка
  unpriced, details «Как считаются показатели?». Старые плитки
  «Стоимость склада, ₽»/«Потенциальная выручка, ₽» удалены из KPI
  (StatsKpi без stock_cost/potential_revenue; _potential_revenue удалена;
  блок «Деньги в складе» по категориям на landed cost не трогали).
- SQL: ~7 запросов независимо от числа видов деталей (2 агрегата остатков,
  1 parts+links, 3 singleton-настройки) + точечные запросы источника цены
  только для деталей с нулевой ценой прайса. Тест лимита: 8.
- Tests: tests/test_warehouse_finance.py (11) + обновлён
  tests/test_statistics.py. Full pytest 892 passed; ruff/djlint/check/
  makemigrations --check чисто. Browser smoke на 375px ok, числа сверены
  вручную.
- Не тронуто: остатки, движения, продажи, резервы, импорты, customs,
  scanner receiving, исторические документы.
