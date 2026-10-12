# Handoff: поиск детали по названию в приёмке и пересчёте (НЕ ГОТОВО)

Ветка `claude/part-name-search-receiving-inventory` от production `7f13b16`.
Статус: WIP, не квалифицировано, не для deploy.

Сделано (код, без тестов):
- `apps/core/part_picker.py`: поиск активных PartType через `apps.catalog.search.search_part_ids`
  (тиры с LIMIT, кириллица через search_name_compact), `looks_like_name_query`.
- `GET /scanner/part-search/?q=` (login + can_manage_inventory), JSON id/name/article/manufacturer.
- Приёмка: action `pick_part` (ID проверяется на сервере, `add_candidate(_warehouse_candidate)`,
  одноразовый `pick_token`); Enter по названию показывает список выбора вместо «Код не распознан».
- Пересчёт: `counting_pick` + `record_part_pick` (через артикул, если он однозначно даёт эту
  карточку, иначе строка `PART-<id>`); `counting_scan` по названию не создаёт «Неизвестную деталь».
- `static/js/part_name_picker.js` (в base.html, инициализация и на denstock:page-loaded),
  `templates/partials/_part_name_pick.html`, CSS.

Осталось:
1. Падает `tests/test_scan_focus.py::test_scan_input_has_autofocus` (вероятно порядок атрибутов
   поля скана); остальные тесты не прогонялись.
2. Написать тесты из задания (1-36): сервис, view, node-тесты JS, PG16, гонки сканера.
3. Браузерная проверка с синтетическими сальниками, мобильная ширина.
4. Полный pytest (SQLite + PG16) против базы 7f13b16, статические проверки.
5. Находка: на партиальной навигации скрипты внутри контента не выполняются, поэтому
   `customer_filter_history.js` (уже в production) и встроенный фильтр клиентов не работают,
   если «Быстрые действия» открыты из бокового меню с уже существующей корзиной. Отдельный фикс.
