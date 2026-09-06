# Customs full catalog-source audit: BLOCKED

Дата: 2026-09-06. Продолжение hotfix с `47ed377`.
Новое правило пользователя: источник полей загруженные каталоги, прежняя
функция `65bd691` больше не ограничивает источники.

## Executive verdict

Полный source audit снимает прежний USD blocker: у SM-01357 и SM-09374
найдены исходные дилерские USD в применённом aftermarket-каталоге.
Однако tracking и страна не содержатся ни в одном обнаруженном загруженном
источнике для всех 84 экспортных строк. Существующий application resolver
также возвращает пустое значение для всех 84 строк.
Это data/contract blocker до реализации и deploy, а не нехватка доступа.
Нельзя заполнить эти поля номером строки, SKU, CANADA или произвольной
категорией и выдать их за каталожный факт.

## Actual state

Повторно выполнены git status, branch, log, fetch origin. Hotfix worktree чист
на старте; main/origin/main, production HEAD и web DENSTOCK_APP_COMMIT:
`34a813bef1c03c6c22c480e87b9ad0261ead7251`.
Production untracked `docker-compose.signing.yml` не изменялся.
Ветка `hotfix/customs-auto-fill-non-weight-fields`, worktree
`/Users/maxinik/Developer/DenStock-customs-hotfix`.
Интернет не использовался. PostgreSQL audits выполнялись в
REPEATABLE READ READ ONLY транзакциях. Файлы открывались только для чтения.

## Catalog-source inventory

Проверены Django registry, фактические таблицы PostgreSQL, все catalog/import
модели, import adapters, CLI importers, private stored files, source_file
provenance каталогов, preset JSON и локальные незарегистрированные XLSX.
Сторонних catalog/fitment/tracking таблиц в фактической схеме не обнаружено.

| Источник/таблица | Строк в production | Доступные бизнес-поля |
| --- | ---: | --- |
| brp_brpcatalogpart | 130519 | material_no/norm, part_desc, last_year_util, brp_status, retail/wholesale USD, replacement 1/2, source_file/row/import_batch/is_current |
| polaris_polariscatalogpart | 130233 | part_number/norm, part_name, superseded, wholesale/retail USD, uom, source_file/row/import_batch |
| catalog_import_aftermarketcatalogpart | 125181 | source, part_id, manufacturer, manufacturer_number/norm, supplier_sku, source_description, msrp_usd, dealer_cost_usd |
| catalog_import_catalogimportbatch | 6 | catalog/status, file name/hash/path, summary/apply_summary, timestamps; не shipping shipment |
| brp_brppartlink / polaris_polarispartlink | 771 / 4 | Явная связь карточки, снимки цены USD и расчёта RUB |
| catalog_parttype / catalog_partnumber | 125981 / 251632 | Имя, категория, производитель; OEM/article, auxiliary alias, internal_ref |
| catalog_manufacturer | 312 | name, country; страна у производителей всех 84 строк пустая |
| catalog_category | 17 | name, parent; у экспортных деталей 82 BRP и 2 Aftermarket, это источник/группа, не тип техники |
| catalog_partcompatibility | 0 | part -> vehicle_model, годы, note; таблица полностью пуста |
| catalog_vehicletype / vehiclemake / vehiclemodel | 6 / 5 / 9 | Справочники техники есть, связь с экспортными деталями отсутствует |
| catalog_partanalog | 1 | Связь двух РАЗНЫХ деталей; у экспортных строк не даёт fitment/tracking/country |
| catalog_partbarcode | 10 | Штрихкод детали, не номер посылки |

## Imported files and snapshots

Все шесть private XLSX доступны. SHA-256 установил три уникальных содержимых:
актуальный BRP, aftermarket 2023 и aftermarket 2026.
Прочитаны ВСЕ строки и ВСЕ листы, в том числе колонки, игнорируемые импортёром.
Дополнительно прочитаны `/opt/denstock/brp.xlsx` и
`/opt/denstock/import/polaris.xlsx` по ссылкам source_file в БД.
Каждый из пяти уникальных XLSX содержит один лист.

| Файл | Лист | Прочитано строк с шапкой | Все фактические поля |
| --- | --- | ---: | --- |
| BRP 2026-08 private | PAA_price_list_260813100438_000 | 131489 | Material_No, Part_Desc, Last_Yr_Util, Status, две ЗАМЕНА НОМЕРА, РОЗНИЦА, ОПТОВАЯ |
| BRP 2026-02 /opt/denstock/brp.xlsx | PAA_price_list_260204090729_000 | 127496 | Те же 8 колонок в другом порядке; J содержит только легенду статусов |
| Polaris /opt/denstock/import/polaris.xlsx | Sheet1 | 130234 | part_number, part_name, superseded_number, ОПТОВАЯ, РОЗНИЦА, uom |
| Aftermarket DEALER 2023 private | priceupdate | 131105 | Manufacturer, Item SKU, Manufacturer Number, Description, MSRP, Dlr Cost |
| Aftermarket DEALER 2026 private | diorlight priceupdate | 125379 | Manufacturer, Item SKU, Manufacturer Number, Description, Dlr Cost |

Партии: #2 BRP applied, #3 коррекция wholesale applied с тем же файлом;
#4/#5 2023 check_failed; #6 2026 applied; #7 тот же 2026 checked.
Статус checked/check_failed не равен применённому справочнику.
Важная тонкость: поле AftermarketCatalogPart.source имеет код `dealer_2023`,
но фактическая применённая партия #6 называется DEALER 2026.
Версию файла определяли по партии, SHA и содержимому, а не по коду source.

Preset compatibility.json содержит одну active связь по имени
«Двигатель Rotax 1203», остальные deferred; ни одна не идентифицирует эти
84 артикула. Название generic детали не использовалось как fuzzy-match.
В DB PartCompatibility нет ни одной строки. Manufacturer presets без стран.
Search synonyms предназначены для поиска, не являются customs fitment mapping.

В исходном локальном checkout также проверен 621 небольшой private XLSX:
612 читаемых, 38 уникальных наборов ячеек, максимум 7 строк на листе;
9 намеренно некорректных файлов. Содержимое совпадает с fixture-наборами
tests/test_analog_catalog_import.py, test_aftermarket_catalog_import.py,
test_aftermarket_sheet_name.py и test_aftermarket_rub_pricing.py.
Локальный db.sqlite3 имеет размер 0; production-import provenance у этих
fixture-файлов нет. Они не объявлялись загруженными production-каталогами.

## Precedence and conflicts

1. Canonical export article сохраняется без изменения. Lookup использует
   существующий normalize_number: убирает пробелы, дефис, _, точку и /,
   приводит к uppercase. Не используется fuzzy или поиск по названию.
2. Exact supplier article проверен во всех трёх catalog tables до aliases.
   Для 84 строк найден ровно один exact catalog identity: 82 BRP, 2 aftermarket.
3. Связь part_id и supplier identity сверена. Auxiliary numbers Kind.ANALOG
   проверены как уже существующие aliases. INTERNAL_REF/SKU проверены при
   аудите источников, но не объявляются номером детали или tracking.
4. Exact имеет приоритет перед alias по apps/core/part_lookup.py:_strong_match.
   PartAnalog не является alias той же детали и не переносит чужие факты.
5. Текущая применённая catalog row является авторитетной; старый или
   check_failed файл не конкурирует с ней как текущий snapshot.
   В BRP importer уже есть правило выбора/сохранения положительной wholesale;
   aftermarket _keep_positive сохраняет прежнюю положительную цену вместо 0.
6. Отсутствие exact USD допускает только существующую replacement/superseded
   price relation. Проверены ВСЕ её пригодные цены; разные цены означали бы
   conflict, а не выбор first(). Фактических конфликтов текущего поля: 0.
   У 420931285 единственный положительный источник 420931284, 19.63 USD;
   артикул экспортной строки остаётся 420931285.

## Column contract and coverage

Это измеренное покрытие ИСТОЧНИКОВ на production read-only snapshot,
не выданный за готовый candidate XLSX и не изменение live exporter.
Все 84 строки, 83 уникальные PartType проверены по отдельности.

| XLSX column | Catalog source/table | Source field | Lookup key | Fallback/precedence | Resolved | Unresolved |
| --- | --- | --- | --- | --- | ---: | ---: |
| A Tracking | Ни один источник | Нет shipment/tracking поля | Canonical article + все сохранённые номера | SKU/import batch не являются посылкой | 0 | 84 |
| B Артикул | Canonical lines + exact catalog identity | material_no / manufacturer_number | Normalized canonical article | Exact прежде aliases, номер не заменять | 84 | 0 |
| C Название RU | BRP / aftermarket + существующий RU_WORDS | part_desc / source_description | Exact article | auto_customs_name_ru, без AI | 84 | 0 |
| D Название EN | BRP / aftermarket | part_desc / source_description | Exact article | 82 BRP + 2 aftermarket, uppercase | 84 | 0 |
| E Производитель | BRP catalog identity / aftermarket manufacturer | BRP / manufacturer.name | Exact article + part link | 82 BRP + 2 SPI; без старого BRP default для SM | 84 | 0 |
| F Страна производства | catalog_manufacturer + raw files checked | country пуст; raw column отсутствует | Manufacturer FK каждой детали | CANADA константа не является catalog source | 0 | 84 |
| G Брутто/шт | Ручной вес | gross_weight_kg | Существующая версия детали | None остаётся blank | manual | allowed |
| H Нетто/шт | Ручной вес | net_weight_kg | Существующая версия детали | None остаётся blank | manual | allowed |
| I Брутто сумма | Формула | G * J | Та же строка | Нужен blank guard при пустом G | derived | weight-dependent |
| J Количество | Canonical completed sale/repair lines | net quantity | Исходный source_key | Не каталог; сохранить 34a813b | 84 | 0 |
| K Стоимость за шт USD | BRP / aftermarket | wholesale_price_usd / dealer_cost_usd | Exact article | 81 BRP direct + 1 BRP replacement + 2 aftermarket | 84 | 0 |
| L Общая стоимость USD | Формула | K * J | Та же строка | Без RUB/cost/rate fallback | 84 | 0 |
| M Область применения | PartCompatibility / все raw metadata | Связи отсутствуют | Part ID, article, approved alias | Существующий mapping не даёт значения | 0 | 84 |

RU coverage означает непустой результат существующего словаря, а не наличие
первичного русского наименования в прайсе. Неизвестные слова словарь сохраняет
на английском; например LENS остаётся LENS. Это отдельное семантическое
ограничение старого перевода, которое счётчик blank не выявляет.

## SM exact evidence: прежний USD blocker снят

| Article | Table / row ID | Applied file / row | Dealer USD | MSRP | Other source fields |
| --- | --- | --- | ---: | --- | --- |
| SM-01357 | catalog_import_aftermarketcatalogpart / 21700 | #6, diorlight priceupdate / 21770 | 203.26 | NULL | manufacturer SPI, SKU 121642, SPI STATOR SKI DOO |
| SM-09374 | catalog_import_aftermarketcatalogpart / 22470 | #6, diorlight priceupdate / 22543 | 127.21 | NULL | manufacturer SPI, SKU 125325, SPI PTO CRANK WEB |

Lookup keys SM01357 / SM09374; part_id 22474 / 23244; manufacturer + normalized
manufacturer number уникальны. BRP/Polaris exact и links отсутствуют,
aftermarket exact найден. _customer_price_rub в aftermarket_catalog.py прямо
описывает dealer_cost_usd как ту же оптовую USD, что у BRP. В customs нужна
сырая dealer_cost_usd, без вызова формулы клиентской цены, курса или наценки.
В 2023 check_failed файле строки 99766/100196 имеют Dlr Cost 208/109.88
и MSRP 334.95/176.95. Это не текущие applied prices; MSRP не использовалась.

## Exact remaining blockers

Для каждого артикула в таблице ниже проверены BRP, Polaris, aftermarket,
сырые пять уникальных прайсов, import metadata, manufacturer/category,
approved aliases, compatibility и preset mappings.

- A: actual header «НОМЕР ТРЕКИНГА/ПОСЫЛКИ». Ни catalog table, ни raw header,
  ни импортная metadata не содержит shipment number. Один номер детали
  не определяет единственную посылку. Supplier SKU и import batch имеют
  другие явно документированные значения. 84/84 объективно неразрешимы.
- F: manufacturer.country пусто у всех 84, в raw файлах нет страны.
  Страна бренда также не доказывает страну производства конкретной детали.
  Прежняя CANADA и более поздняя КАНАДА являются business defaults, а не
  значениями загруженных каталогов. 84/84 неразрешимы по новому source rule.
- M: raw fields не содержат fitment/model/product group. Last_Yr_Util
  является годом, UOM единицей упаковки, а категории BRP/Aftermarket источником
  каталога. Они не задают область применения детали. Для 83 строк описание
  также не даёт однозначной связи с загруженной техникой.
  Особый случай SM-01357: текст SPI STATOR SKI DOO содержит явную подсказку
  Ski-Doo; загруженный VehicleMake Ski-Doo связан со снегоходом. Это возможный
  один случай нового literal-description rule, но существующий importer/resolver
  такую связь не создаёт. Он не устраняет отсутствие данных у остальных 83
  строк и не снимает A/F gate. Не заявляем, что в этой строке нет подсказки.

Построчный индекс доказательств. Таблицы B/A в Source означают BRP/Aftermarket;
Polaris exact проверен для каждой строки и дал 0. A/F блокируют каждую строку.
Полный перечень доступных source fields приведён в inventory выше.

| Article | Exact normalized key | Source table / ID | Approved aliases | Application evidence |
| --- | --- | --- | --- | --- |
| 219704404 | 219704404 | B / 6097 | нет | нет связи/однозначного metadata |
| 250400101 | 250400101 | B / 10103 | 420229202 | нет связи/однозначного metadata |
| 293300026 | 293300026 | B / 26594 | 420850540 | нет связи/однозначного metadata |
| 293350074 | 293350074 | B / 26739 | нет | нет связи/однозначного metadata |
| 293350150 | 293350150 | B / 26755 | 293350126 | нет связи/однозначного metadata |
| 293650100 | 293650100 | B / 27016 | нет | нет связи/однозначного metadata |
| 293650138 | 293650138 | B / 27039 | 293650345 | нет связи/однозначного metadata |
| 415129349 | 415129349 | B / 30390 | нет | нет связи/однозначного metadata |
| 415129781 | 415129781 | B / 30673 | нет | нет связи/однозначного metadata |
| 415130430 | 415130430 | B / 31107 | 415129375 | нет связи/однозначного metadata |
| 415130651 | 415130651 | B / 31232 | 415130154 | нет связи/однозначного metadata |
| 417127016 | 417127016 | B / 31635 | 420629211 | нет связи/однозначного metadata |
| 417127294 | 417127294 | B / 31710 | нет | нет связи/однозначного metadata |
| 417223021 | 417223021 | B / 31923 | 417222107 | нет связи/однозначного metadata |
| 417300551 | 417300551 | B / 32348 | 417300367 | нет связи/однозначного metadata |
| 420210646 | 420210646 | B / 32692 | 420210641 | нет связи/однозначного metadata |
| 420230515 | 420230515 | B / 33047 | нет | нет связи/однозначного metadata |
| 420232100 | 420232100 | B / 33066 | 5232100 | нет связи/однозначного metadata |
| 420233960 | 420233960 | B / 33150 | 420233962 | нет связи/однозначного metadata |
| 420233965 | 420233965 | B / 33152 | 420233967 | нет связи/однозначного metadata |
| 420233995 | 420233995 | B / 33155 | 420233997 | нет связи/однозначного metadata |
| 420256348 | 420256348 | B / 33940 | 461611 | нет связи/однозначного metadata |
| 420256915 | 420256915 | B / 34012 | нет | нет связи/однозначного metadata |
| 420430037 | 420430037 | B / 34910 | 420430036 | нет связи/однозначного metadata |
| 420430040 | 420430040 | B / 34911 | нет | нет связи/однозначного metadata |
| 420430054 | 420430054 | B / 34919 | 420430053 | нет связи/однозначного metadata |
| 420431402 | 420431402 | B / 35023 | 420431401 | нет связи/однозначного metadata |
| 420440568 | 420440568 | B / 35252 | 420440567 | нет связи/однозначного metadata |
| 420630642 | 420630642 | B / 36188 | 420630643 | нет связи/однозначного metadata |
| 420631486 | 420631486 | B / 36225 | 5631486 | нет связи/однозначного metadata |
| 420631610 | 420631610 | B / 36230 | 420631612, 462597 | нет связи/однозначного metadata |
| 420831849 | 420831849 | B / 38216 | 831843 | нет связи/однозначного metadata |
| 420831955 | 420831955 | B / 38229 | 420831957 | нет связи/однозначного metadata |
| 420832030 | 420832030 | B / 38233 | нет | нет связи/однозначного metadata |
| 420832176 | 420832176 | B / 38240 | 420832174 | нет связи/однозначного metadata |
| 420832178 | 420832178 | B / 38241 | нет | нет связи/однозначного metadata |
| 420832420 | 420832420 | B / 38258 | нет | нет связи/однозначного metadata |
| 420832445 | 420832445 | B / 38265 | 420832442 | нет связи/однозначного metadata |
| 420832672 | 420832672 | B / 38312 | 420832602 | нет связи/однозначного metadata |
| 420845106 | 420845106 | B / 38609 | нет | нет связи/однозначного metadata |
| 420845467 | 420845467 | B / 38624 | нет | нет связи/однозначного metadata |
| 420845580 | 420845580 | B / 38628 | 5845580 | нет связи/однозначного metadata |
| 420850552 | 420850552 | B / 38693 | 850552 | нет связи/однозначного metadata |
| 420853023 | 420853023 | B / 38819 | 5853023 | нет связи/однозначного metadata |
| 420856536 | 420856536 | B / 38955 | 461607 | нет связи/однозначного metadata |
| 420867105 | 420867105 | B / 39106 | нет | нет связи/однозначного metadata |
| 420892388 | 420892388 | B / 39789 | 420889187 | нет связи/однозначного metadata |
| 420893797 | 420893797 | B / 39930 | 420893796 | нет связи/однозначного metadata |
| 420898042 | 420898042 | B / 40004 | 462542 | нет связи/однозначного metadata |
| 420916412 | 420916412 | B / 40173 | 420916411 | нет связи/однозначного metadata |
| 420931284 | 420931284 | B / 40456 | 420931285 | нет связи/однозначного metadata |
| 420931285 | 420931285 | B / 40457 | 420931285 | нет связи/однозначного metadata |
| 420931410 | 420931410 | B / 40470 | 290931410 | нет связи/однозначного metadata |
| 420931455 | 420931455 | B / 40473 | нет | нет связи/однозначного metadata |
| 420931542 | 420931542 | B / 40479 | нет | нет связи/однозначного metadata |
| 420931568 | 420931568 | B / 40482 | 420931566 | нет связи/однозначного metadata |
| 420931590 | 420931590 | B / 40495 | нет | нет связи/однозначного metadata |
| 420931795 | 420931795 | B / 40526 | 420931793 | нет связи/однозначного metadata |
| 420931811 | 420931811 | B / 40529 | 420931810 | нет связи/однозначного metadata |
| 420933180 | 420933180 | B / 40608 | 461645 | нет связи/однозначного metadata |
| 420933442 | 420933442 | B / 40615 | 462425 | нет связи/однозначного metadata |
| 420933456 | 420933456 | B / 40617 | 420933455 | нет связи/однозначного metadata |
| 420933466 | 420933466 | B / 40619 | 462423 | нет связи/однозначного metadata |
| 420950089 | 420950089 | B / 40884 | 420950087 | нет связи/однозначного metadata |
| 420950772 | 420950772 | B / 40920 | 462517 | нет связи/однозначного metadata |
| 420950840 | 420950840 | B / 40926 | 5950840 | нет связи/однозначного metadata |
| 420956675 | 420956675 | B / 41053 | 462464 | нет связи/однозначного metadata |
| 508000609 | 508000609 | B / 65077 | нет | нет связи/однозначного metadata |
| 509000045 | 509000045 | B / 65397 | 293650333 | нет связи/однозначного metadata |
| 512061507 | 512061507 | B / 67872 | 512060387 | нет связи/однозначного metadata |
| 513034047 | 513034047 | B / 68216 | 513034127, 513034279 | нет связи/однозначного metadata |
| 514054529 | 514054529 | B / 68537 | нет | нет связи/однозначного metadata |
| 514055203 | 514055203 | B / 68704 | нет | нет связи/однозначного metadata |
| 5446652 | 5446652 | B / 82643 | M5446652 | нет связи/однозначного metadata |
| 703500875 | 703500875 | B / 85586 | 706204623 | нет связи/однозначного metadata |
| 705400036 | 705400036 | B / 95400 | 705401648 | нет связи/однозначного metadata |
| 705400928 | 705400928 | B / 95625 | нет | нет связи/однозначного metadata |
| 705401093 | 705401093 | B / 95650 | нет | нет связи/однозначного metadata |
| 706200653 | 706200653 | B / 100373 | 706200091 | нет связи/однозначного metadata |
| 715900118 | 715900118 | B / 117193 | нет | нет связи/однозначного metadata |
| 732401030 | 732401030 | B / 118057 | 430730 | нет связи/однозначного metadata |
| 861805547 | 861805547 | B / 120372 | 503195351 | нет связи/однозначного metadata |
| SM-01357 | SM01357 | A / 21700 | нет | SKI DOO в описании; mapping отсутствует |
| SM-09374 | SM09374 | A / 22470 | нет | нет связи/однозначного metadata |

## Reconciliation and release status

После завершения аудита production customs_reconcile --json повторён:
117 source lines, 115 effective, 2 fully returned; 84 aggregate rows;
canonical quantity = aggregate quantity = report quantity = 199.000;
canonical client amount = report amount = 697122.00 RUB.
delta_quantity=0.000, delta_amount=0.00, report_only=0, customs_only=0,
silent=0, duplicates=0, RECONCILED. Canonical universe не изменялся.
В XLSX нет колонки RUB: K/L содержат USD. Нельзя выдавать 697122 RUB
за пересчитанную сумму L.

Покрытие snapshot не равно live результату: runtime всё ещё 34a813b.
Candidate hotfix XLSX не создавался, поскольку обязательные source fields
отсутствуют. Нельзя объявлять их заполненными или разворачивать частичный
exporter при явно заданном пользователем запрете deploy с любым blank.
Production XLSX baseline: A84 B0 C84 D84 E79 F79 G84 H84 K84 M84 blanks;
I/L содержат формулы, J заполнен. Сам exporter пока не исправлен.

Изменены только audit/handoff документы. Нет runtime, DB, schema,
stock или history изменений. PRE/POST backup и deploy не выполнялись.
pytest/ruff/djlint/Django gates не запускались, поскольку задача остановлена
на source gate до реализации. git diff --check выполнен для документации.

ARE ALL NON-WEIGHT FIELDS NOW POPULATED FROM LOADED CATALOGS: NO.
IS WEIGHT THE ONLY MANUAL INPUT IN LIVE EXPORT: NO.
DOES LIVE HISTORICAL EXPORT RECONCILE EXACTLY WITH REPORT: YES.

## Evidence and continuation

Read-only scripts, schema/source inventory, exact per-row matches, raw file
headers/matches/SHA и повторная live reconciliation сохранены локально:
`/Users/maxinik/Developer/DenStock-customs-evidence-20260906`.
Коммерческие исходные XLSX и полные dumps в Git не добавлены.
Эти файлы доступны для следующего исполнителя без повторения аудита.

Для снятия A/F blocker требуется новый фактический shipment/country источник
либо явное изменение column/source contract. Для M нужен загруженный
article-to-application mapping; одна подсказка Ski-Doo не покрывает остальные.
После снятия blockers реализовать population с exact-first и field-conflict
fail-closed, включая aftermarket dealer USD и blank-safe weight formula,
сохранить canonical source_key, добавить регрессии, выполнить полный gate,
fresh snapshot candidate и условный signed PRE/deploy/live/POST workflow.

## SHA-256 source registry

- `/app/private_media/catalog-imports/20260818-095347-6c6a98a0.xlsx`: `7ebbe77e4b18b72d0dac44f4568782d43a7f8f6e99e045d6b6044c3cea10b8de`
- `/app/private_media/catalog-imports/20260825-101508-e84642c4.xlsx`: `8e3ff3ade40214e0b6f8bd44a2892ffb9ce4467d1722e4b185c170a0574dd93b`
- `/app/private_media/catalog-imports/20260827-011757-f43e9817.xlsx`: `49820b9ddb2fa1236a07e64823ab26e27f36268dc25031945922f6934832bc1b`
- `/tmp/denstock-audit-brp.xlsx`: `c9ea6e330b39957d76141fd13b7a251c1b08bd6634517f0f6b2f34a4320c5e62`
- `/tmp/denstock-audit-polaris.xlsx`: `2940079b0be9ef68c1de18206342b8f7f41a21acb5d4e6c1da255730628d5cb4`
