# Customs auto-fill forensic gate: BLOCKED

Дата: 2026-09-06. Классификация задачи: hotfix; остановлена на forensic gate до изменения runtime.

## Независимая проверка

После git fetch origin локальный main, origin/main, production HEAD и
DENSTOCK_APP_COMMIT работающего web совпали:
`34a813bef1c03c6c22c480e87b9ad0261ead7251`.
Локальный исходный checkout чист. На сервере уже существовал untracked
`docker-compose.signing.yml`; он не изменялся.
Создан fresh worktree `/Users/maxinik/Developer/DenStock-customs-hotfix`,
ветка `hotfix/customs-auto-fill-non-weight-fields`.

## Previous Correct Version

Последняя версия перед удалением прежнего auto-fill:
`65bd6910d36a1d919bd01e6f0d4c16abb8f53cdf`, parent `647e3a9`.
`647e3a9` заменил population на сохранённые таможенные факты.
Версия, выполнявшая ВСЁ новое требование, не установлена: tracking
оставался ручным с первого exporter `2c43365`, область применения
допускала отсутствие, USD без связи каталога также оставался пустым.
`2105e45` уже использовал compatibility mapping вместо legacy МОТО ЗАПЧАСТИ.
Возврат legacy константы не является восстановлением последней логики.

Для сравнения извлечены AST-функции population, чтения карточки, перевода,
catalog lookup, wholesale fallback и XLSX writer непосредственно из `65bd691`.
Они выполнены в отдельном namespace внутри production Django shell,
в PostgreSQL REPEATABLE READ READ ONLY транзакции.
Оба writer получили ОДНИ И ТЕ ЖЕ 84 canonical aggregate rows из `34a813b`.
У старого writer заменён только вход build_export_rows на эти готовые строки;
его movement/action universe не исполнялся. Количества не реконструировались.
Файлы получены через stdout/SSH в память и сохранены локально;
HTTP download endpoint в этой проверке не использовался.

## Column Contract

Счётчики: populated/blank среди 84 строк 10..93. Формула считается
заполненной ячейкой, но это не доказывает наличие числового результата.

| Колонка | Прежний источник | Текущий источник | Ожидаемый источник | Авто/ручное | Production populated/blank | Previous replay populated/blank |
| --- | --- | --- | --- | --- | --- | --- |
| A: Номер трекинга/посылки | None, ручной ввод с первого exporter | None | Источник отсутствует; BLOCKED | ранее ручное | 0/84 | 0/84 |
| B: Артикул/парт номер | Точный snapshot действия, иначе identity_number | Доказанный номер canonical line | Сохранить canonical number | авто | 84/0 | 84/0 |
| C: Название RU | Сохранённое RU, иначе RU_WORDS от EN | RU сохранённой версии или пусто | Прежний RU fallback | авто | 0/84 | 84/0 |
| D: Название EN | Polaris.part_name; иначе BRP.part_desc; fallback PartType.name | EN сохранённой версии или пусто | Прежний каталог/fallback | авто | 0/84 | 84/0 |
| E: Производитель | Snapshot действия; иначе карточка, Polaris/BRP defaults | Производитель сохранённой версии или пусто | Прежний источник; проверить legacy defaults | авто | 5/79 | 84/0 |
| F: Страна производства | Константа CANADA | Страна сохранённой версии или пусто | Восстановление исторического правила CANADA | авто | 5/79 | 84/0 |
| G: Вес брутто/шт | PartCustomsInfo.gross_weight_kg | gross_weight_kg версии | Ручной вес; отсутствие = blank | ручное | 0/84 | 0/84 |
| H: Вес нетто/шт | PartCustomsInfo.net_weight_kg | net_weight_kg версии | Ручной вес; отсутствие = blank | ручное | 0/84 | 0/84 |
| I: Вес брутто сумма | =Jr*Gr | =Jr*Gr | Формула с blank guard при отсутствии G | расчёт | 84/0 | 84/0 |
| J: Количество/шт | Старый агрегатор WarehouseAction | Canonical net quantity | Сохранить новый canonical universe | авто | 84/0 | 84/0 |
| K: Стоимость за шт, USD | BRP wholesale, иначе replacement; Polaris wholesale/superseded | USD сохранённой версии или пусто | Прежний wholesale rule; 2 строки BLOCKED | авто | 0/84 | 82/2 |
| L: Общая стоимость, USD | =Kr*Jr | =Kr*Jr | Формула; отсутствие K не является корректным нулём | расчёт | 84/0 | 84/0 |
| M: Область применения | Сохранённая область; иначе PartCompatibility mapping | Область версии; compatibility только при наличии версии | Прежний mapping; 84 строки BLOCKED | авто при наличии источника | 0/84 | 0/84 |

## Exact blockers

1. A, tracking: 84 строки, все артикулы ниже. Прежний источник не исчез:
   его не было, exporter явно писал None и ожидал ручной номер посылки.
   Номер продажи или порядковый номер строки не является tracking.
2. M, область применения: 84 строки, все артикулы ниже. Все сохранённые
   application_area пусты; resolve_customs_application возвращает пустую
   строку для каждой детали. Прежний источник: PartCompatibility ->
   VehicleModel -> VehicleMake -> VehicleType, либо явное значение карточки.
   Нет доступного результата по прежнему правилу. Доказательств того, что
   эти сведения были удалены из production, нет.
3. K, USD: 2 строки, `SM-01357` (part 22474), `SM-09374` (part 23244).
   Нет BrpPartLink и PolarisPartLink. Прежняя функция не поддерживала
   aftermarket как источник USD. Цена склада/клиента и новый перевод RUB
   в USD запрещены постановкой; такой fallback не добавлялся.
   Наличие цены другого назначения не устраняет отсутствие прежнего источника.

Историческое USD-правило: положительная wholesale самой BRP позиции;
иначе первая по pk текущая BRP позиция с положительной wholesale,
связанная replacement в любую сторону. Для Polaris аналогично superseded,
без фильтра is_current. Replacement меняет только источник цены, не артикул.
Строки обрабатывались strip/upper; RU_WORDS переводит известные слова,
неизвестные оставляет. F было CANADA, более позднее system_customs_facts
использует КАНАДА, но это другой путь и не прежняя export population.
Старый manufacturer fallback BRP и EN fallback PartType.name сами по себе
не подтверждают корректность производителя/английского языка для aftermarket.

Артикулы с блокерами A/M:

219704404, 250400101, 293300026, 293350074, 293350150, 293650100, 293650138, 415129349, 415129781, 415130430, 415130651, 417127016, 417127294, 417223021, 417300551, 420210646, 420230515, 420232100, 420233960, 420233965, 420233995, 420256348, 420256915, 420430037, 420430040, 420430054, 420431402, 420440568, 420630642, 420631486, 420631610, 420831849, 420831955, 420832030, 420832176, 420832178, 420832420, 420832445, 420832672, 420845106, 420845467, 420845580, 420850552, 420853023, 420856536, 420867105, 420892388, 420893797, 420898042, 420916412, 420931284, 420931285, 420931410, 420931455, 420931542, 420931568, 420931590, 420931795, 420931811, 420933180, 420933442, 420933456, 420933466, 420950089, 420950772, 420950840, 420956675, 508000609, 509000045, 512061507, 513034047, 514054529, 514055203, 5446652, 703500875, 705400036, 705400928, 705401093, 706200653, 715900118, 732401030, 861805547, SM-01357, SM-09374.

## Weight

Ручные G/H пусты 84/84 в обоих XLSX, без записанного 0. Экспорт доступен.
I содержит =J*G и при пересчёте Excel даст 0 при пустом G: будущему hotfix
нужен blank guard. Формулы L при пустом K также не доказывают стоимость.
Проверка подсчитывала реальные значения/formulas из сохранённых XLSX,
не cached values и не только названия полей.

## Live Historical Reconciliation

`customs_reconcile --json` на production: RECONCILED.

| Показатель | Значение |
| --- | --- |
| Canonical source lines | 117 |
| Effective lines | 115 |
| Fully returned lines | 2 |
| Canonical quantity | 199.000 |
| Canonical client amount RUB | 697122.00 |
| XLSX aggregate rows | 84 |
| Aggregate quantity | 199.000 |
| Report client amount RUB | 697122.00 |
| delta_quantity | 0.000 |
| delta_amount | 0.00 |
| report_only / customs_only | 0 / 0 |
| silent / duplicates | 0 / 0 |

В реальном XLSX нет рублёвого столбца: K/L являются USD. 697122.00 RUB
является контрольной суммой canonical lines против отчёта, а не суммой L.

## Safety and completion

Runtime, schema, stock и исторические данные не изменялись. Все запросы
сравнения защищены READ ONLY транзакцией. Main не изменён, deploy не запускался.
201-unit bug не возвращён. Signed PRE/POST backup, deploy, live smoke после
deploy и business fingerprint comparison не выполнялись: pre-deploy gate BLOCKED.
Полный pytest/ruff/djlint/Django gate не запускался: исправления поведения нет,
задача НЕ завершена, сформирован handoff. Нельзя считать это готовым hotfix.

ARE ALL NON-WEIGHT CUSTOMS COLUMNS AUTO-FILLED: NO.
IS WEIGHT THE ONLY MANUAL FIELD: NO (tracking исторически ручной).
DOES LIVE HISTORY EXACTLY RECONCILE WITH REPORT: YES.

Для продолжения нужны источник tracking (либо явное исключение A из
контракта), подтверждённый application mapping для перечисленных деталей
и разрешённый источник USD для двух SM-артикулов. Затем port только population,
регрессии по постановке, все пять checks, fresh snapshot gate и только после
PASS signed backup/deploy workflow из постановки пользователя.
