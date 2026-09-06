# «Запчасти на заказ»: как влить после таможенного hotfix

Ветка: `feature/ordered-parts`, база `c517b6c` (`origin/main` на момент старта).

Пока раздел делался, второй агент выпустил таможенный hotfix и выложил его на
production. Поэтому вливать надо не в `origin/main`, а поверх выпущенного
таможенного состояния.

## Что менялось у соседа

`hotfix/customs-auto-fill-non-weight-fields`: RU-названия и единый стиль XLSX,
разделение выгрузки на обычную и аналоговую (`historical_analog_customs_rows`,
маршрут `actions/export/analogs/`), формат весов, страна CANADA латиницей.

## Единственный конфликт

Пробный merge выполнен и проверен. Конфликтует **один файл, один хунк**:

`apps/actions/services.py` -> тело `historical_customs_rows`.

Обе стороны дописали в одну функцию: сосед отфильтровал аналоги, раздел заказов
добавил свои строки. Резолюция механическая и уже проверена:

```python
def historical_customs_rows(
    *, date_from=None, date_to=None, action_type="", q="", part_number="", location_code="",
) -> list[dict]:
    filters = {
        "date_from": date_from, "date_to": date_to, "action_type": action_type,
        "q": q, "part_number": part_number, "location_code": location_code,
    }
    rows = _customs_rows_from_lines(
        [line for line in canonical_customs_lines(**filters) if not line.get("is_analog")]
    )
    # Заказанные детали оригинальные, поэтому идут в обычную выгрузку и только
    # в неё. В выгрузку аналогов они не добавляются ни при каких условиях.
    return rows + ordered_customs_rows(**filters)
```

`historical_analog_customs_rows` остаётся ровно такой, какой её написал сосед.
Добавлять в неё заказы НЕЛЬЗЯ: раздел оформляет только оригиналы.

Функция `ordered_customs_rows` из этой ветки переносится следом без изменений.

После резолюции на пробном merge прошли 347 тестов обеих сторон, включая
`test_customs_ru_name_and_style.py`, тесты разделения аналогов и обе новые
сюиты раздела. `ruff` и `manage.py check` чистые.

## Файлы, которые может задеть таможенный hotfix

| файл | что делает эта ветка | риск |
|---|---|---|
| `apps/actions/services.py` | константы происхождения, поле `provenance` у строки, `ordered_customs_rows`, зелёная заливка артикула | **конфликт в одном хунке, см. выше** |
| `apps/actions/customs_history.py` | не трогается | нет |
| `apps/actions/views.py` | не трогается | нет |
| `templates/actions/report.html` | не трогается | нет |
| тесты таможни соседа | не трогаются | нет |

Ключ строки (`source_key`) у продаж и ремонтов НАМЕРЕННО оставлен прежним
трёхэлементным: сосед читает `source_key[0]` как идентификатор детали. Строки
заказов носят ключ из четырёх элементов, а кортежи разной длины не равны
никогда, поэтому столкнуться две строки одного артикула не могут.

## Порядок вливания

1. дождаться, пока таможенный hotfix попадёт в `main`;
2. `git rebase main` на `feature/ordered-parts` (или merge - конфликт тот же);
3. разрешить единственный хунк по образцу выше;
4. прогнать: `tests/test_ordered_parts.py`,
   `tests/test_ordered_parts_customs.py`, все `tests/test_customs_*`,
   `tests/test_navigation_simplification.py`;
5. `ruff`, `manage.py check`, `makemigrations --check`, `djlint`.

## Что ещё ждёт решения пользователя

- статусы заказа («Оформлен / Заказан / Получен / Выдан / Отменён») в V1 не
  реализованы намеренно;
- удаления заказа нет: правятся только клиент и предоплата. Ошибка в самой
  детали закрывается новым заказом. Нужен ли отзыв записи - продуктовый вопрос;
- количество в записи отсутствует: одна запись это одна единица;
- заказ аналога запрещён. Если бизнес захочет заказывать аналоги, менять надо
  одну функцию `apps/ordered_parts/services.is_aftermarket_part` и решить, в
  какую из двух выгрузок такие строки идут.
