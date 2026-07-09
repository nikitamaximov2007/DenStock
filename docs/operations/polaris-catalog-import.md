# Polaris-каталог: импорт дилерского прайса

Файл-источник: `POLARIS DEALER PRICE 2023 ATV, UTV (1).xlsx`. Файл в Git не
коммитится.

Polaris-прайс это справочник, а не складской остаток. Импорт не создаёт
карточки склада, поступления, движения, продажи или балансы.

## Формат файла

- Один лист.
- Заголовки в первой строке строго такие:
  `part_number`, `part_name`, `superseded_number`, `ОПТОВАЯ`, `РОЗНИЦА`, `uom`.
- Данные начинаются со второй строки.
- `part_number` хранится строкой, без `int(part_number)`.
- `РОЗНИЦА` и `ОПТОВАЯ` читаются как Decimal. Пустые и нулевые цены не ошибка.
- `superseded_number` используется для поиска и как возможный источник цены,
  но exact `part_number` всегда остаётся identity детали.

## Команды

Dry-run по умолчанию ничего не пишет:

```bash
python manage.py import_polaris_catalog --file "/path/to/POLARIS DEALER PRICE 2023 ATV, UTV (1).xlsx" --dry-run
```

Запись только с явным флагом:

```bash
python manage.py import_polaris_catalog --file "/path/to/POLARIS DEALER PRICE 2023 ATV, UTV (1).xlsx" --commit
```

Команда идемпотентна: повторный импорт обновляет существующие строки по
`part_number`, но не создаёт дубли.

Отчёт команды показывает: строк просмотрено, строк данных, создано, обновлено,
пропущено без изменений, пустые строки, строки без retail-цены,
строки с `superseded_number`, количество ошибок и примеры ошибок.

## Цена

Настройки Polaris отдельные от BRP:

```text
цена клиента = розница USD x курс Polaris x (1 + наценка Polaris / 100)
```

Итог округляется до целого рубля `ROUND_HALF_UP`. Термин в UI: «Наценка».

Если у exact part_number розница 0, система может взять цену из связанной
superseded/source позиции с ненулевой розницей. Номер в пересчёте, продаже,
отчёте и Excel остаётся exact part_number.

## Production import

```bash
cd /opt/denstock

docker compose exec -T web python manage.py backup_all
docker compose exec -T web python manage.py ops_check

docker compose exec -T web python manage.py import_polaris_catalog --file "/opt/denstock/import/POLARIS DEALER PRICE 2023 ATV, UTV (1).xlsx" --dry-run
```

Если dry-run нормальный:

```bash
docker compose exec -T web python manage.py backup_all
docker compose exec -T web python manage.py import_polaris_catalog --file "/opt/denstock/import/POLARIS DEALER PRICE 2023 ATV, UTV (1).xlsx" --commit
docker compose exec -T web python manage.py ops_check
docker compose exec -T web python manage.py backup_all
```

## Ручная проверка

1. `/polaris/` открывается.
2. Поиск номеров из файла работает, например `3610030` или `3022082`.
3. Цена клиента считается по настройкам Polaris.
4. Общий поиск показывает Polaris отдельно от BRP. Если номер есть в обоих
   каталогах, видны оба варианта.
5. В «Инвентаризации ячейки» скан Polaris создаёт строку с источником
   «Polaris-каталог».
6. После проведения пересчёта остаток появляется в выбранной ячейке.
7. В «Действиях со склада» проданная Polaris-деталь сохраняет exact part_number.
8. В Excel для таможни колонка B содержит exact номер, колонка E содержит
   `POLARIS`, колонка F остаётся пустой, если страна не заполнена вручную.

