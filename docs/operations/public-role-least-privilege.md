# Роль `denstock_public`: только нужные столбцы

## Проблема (проверено на production 2026-10-13, только чтение)

Роль публичного каталога (`catalog-web`) имела SELECT на ВСЕ столбцы таблиц
`inventory_stocklot`, `inventory_partitem`, `procurement_batch`,
`sales_reservation`, `brp_*`, `polaris_*`, `actions_partcustomsinfo`. Если
процесс публичного каталога будет взломан, через эту роль читаются:

- себестоимость партий и единиц (`landed_unit_cost_rub`, `landed_cost_rub`);
- расходы и поставщик закупки (`procurement_batch`: доставка, таможня,
  комиссия, номер счета, поставщик, сотрудник);
- оптовые цены поставщика в USD, курс и наценка (`brp_*`, `polaris_*`);
- таможенная стоимость (`customs_unit_price_usd`);
- имя и телефон клиента в резерве (`sales_reservation`; сегодня 0 строк).

## Решение

Для этих таблиц роль получает SELECT на все столбцы, КРОМЕ перечисленных в
`apps/inventory/public_columns.py` (`HIDDEN_COLUMNS`). Скрипт
`scripts/operations/create_public_catalog_role.sql` выдает права по столбцам
из `information_schema`, поэтому новые безопасные столбцы после миграций
появляются автоматически, а скрытые остаются скрытыми. Тест держит списки в
Python и SQL одинаковыми.

Код, который выполняется под этой ролью и загружает строки этих таблиц,
откладывает (`defer`) ровно скрытые столбцы:

- `available_totals` (наличие в каталоге) вызывает `live_stock_rows(public_columns_only=True)`;
  внутренние экраны склада по-прежнему загружают строки целиком;
- `build_public_part_facts` и `create_customer_request` не загружают оптовые
  цены, курс и наценку из связей BRP и Polaris.

Публичная цена берется только из `PartType.recommended_price`; оптовые цены,
курс и наценка ей не нужны (проверено по коду `apps/inventory/pricing.py`).

## Доказательства (PostgreSQL 16, изолированно)

- Все публичные страницы, поиск, карточки, наличие, корзина и отправка
  заявки (включая MAX и Telegram) работают под ролью с новыми правами
  (`tests/test_public_catalog_role_postgresql.py`, `tests/test_customer_account_postgresql.py`).
- Под этой ролью чтение 10 образцов скрытых столбцов отклоняется PostgreSQL
  ("permission denied"), а количество и цена для клиента читаются.
- `live_stock_rows` с `public_columns_only=True` дает те же количества, что и
  без него.

## Порядок выпуска (важно)

Код и права выпускаются ВМЕСТЕ с обновлением `catalog-web`, и права
применяются ПОСЛЕ него:

1. Сейчас `catalog-web` работает на образе от 2026-09-28. Его код загружает
   строки целиком и с новыми правами получит "permission denied": каталог
   покажет "временно недоступен". Поэтому права НЕЛЬЗЯ применить отдельно.
2. Выпуск: пересобрать `web` и `catalog-web` с этим кодом (он работает и со
   старыми, и с новыми правами), убедиться, что каталог работает.
3. Затем от имени владельца БД:
   `docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < scripts/operations/create_public_catalog_role.sql`
4. Проверка: главная, поиск, карточка с наличием, корзина, заявка;
   `SELECT landed_unit_cost_rub FROM inventory_stocklot LIMIT 1` от имени
   `denstock_public` дает "permission denied".
5. Откат прав, если каталог сломался: применить скрипт из предыдущего
   коммита (он снова выдаст права на таблицы целиком). Данные не меняются,
   простоя нет.

Выпуск каталога тянет и другие изменения `catalog-web` с 2026-09-28; это
решение владельца и отдельная проверка (см. отчет дня).
